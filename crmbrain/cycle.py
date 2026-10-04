from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timedelta

from crmbrain import (
    briefing,
    calendar_events,
    enrichment,
    intelligence,
    intent,
    policy,
    prune,
    reconcile,
    slack_notify,
    staleness,
    ticker,
)
from crmbrain.budget import WriteBudget
from crmbrain.config import (
    NO_SHOW_HINT,
    STAGE,
    Settings,
    compute_lookback_start,
    is_excluded_contact,
    is_josh_address,
    is_non_deal_person,
    is_personal,
    is_personal_family_intent,
    now_utc,
    settings_lookback_start,
)
from crmbrain.gmail_client import Gmail
from crmbrain.google_auth import drive_auth_detail, has_drive_access
from crmbrain.heyreach import HeyReach
from crmbrain.hubspot import HubSpot
from crmbrain.memory import Memory
from crmbrain.deal_write import authorize_deal_write, commit_amount_write, commit_deal_write, propose_deal_write
from crmbrain.policy import INCREMENT_NO_SHOW
from crmbrain.models import CycleReport, Engagement
from crmbrain.leadmagic import should_skip_email, usable_linkedin
from crmbrain.sources import cube_acr, fireflies, gmail_scan, rvm, smartlead
from crmbrain.sources.cube_acr import CubeAuthError
from crmbrain.sources.gmail_scan import is_junk_crm_email

logger = logging.getLogger(__name__)


def _in_window(ev: Engagement, settings: Settings) -> bool:
    extra = ev.extra or {}
    # Upcoming Calendar / GCal creates are scanned every cycle, not by lookback.
    if extra.get("gcal_create") or extra.get("create_new") or extra.get("skip_lookback"):
        return True
    if ev.occurred_at and ev.occurred_at > now_utc():
        return True
    start = settings_lookback_start(settings)
    if ev.source == "smartlead":
        # Positive replies stay on the ticker. First cycle still respects
        # lookback so we do not dump the whole history.
        if ev.occurred_at and ev.occurred_at < start:
            return False
    if ev.occurred_at and ev.occurred_at < start:
        return False
    return True


def _release_gmail_people_overflow(memory: Memory, ev: Engagement) -> None:
    extra = ev.extra or {}
    if ev.source != "gmail_person" and not extra.get("gmail_overflow"):
        return
    email = (ev.email or "").strip().lower()
    if email and hasattr(memory, "drop_gmail_people_overflow"):
        memory.drop_gmail_people_overflow(email)


def _queue_cap_review(memory: Memory, report: CycleReport, ev: Engagement) -> None:
    label = ev.display_name() or ev.email or ev.phone or ev.external_id
    line = f"{label} cap"
    if line not in report.review_queue:
        report.review_queue.append(line)
    if hasattr(memory, "enqueue_review"):
        memory.enqueue_review(
            {
                "person_key": ev.email or ev.phone or ev.display_name() or ev.external_id,
                "email": ev.email,
                "name": ev.display_name(),
                "company": ev.company,
                "reason": "cap",
                "evidence": {"source": ev.source, "subject": ev.raw_subject},
            }
        )


def _reserve_budget(
    budget: WriteBudget | None,
    kind: str | None,
    memory: Memory,
    report: CycleReport,
    ev: Engagement,
) -> bool:
    """Reserve one create, stage_move, or amount write. Overflow → review_queue reason cap."""
    if not kind:
        return True
    if budget is None:
        return True
    if budget.aborted or not budget.allow(kind):
        _queue_cap_review(memory, report, ev)
        return False
    return True


def _contact_deals(hs: HubSpot, contact: dict | None) -> list[dict]:
    if not contact or not contact.get("id"):
        return []
    try:
        return hs.open_deals_for_contact(contact["id"]) or []
    except Exception:
        return []


def _company_deals(hs: HubSpot, ev: Engagement, contact: dict | None) -> list[dict]:
    props = (contact or {}).get("properties") or {}
    company = (ev.company or props.get("company") or "").strip()
    if not company or not hasattr(hs, "deals_for_company"):
        return []
    try:
        return hs.deals_for_company(company) or []
    except Exception:
        return []


def _mark_closed_won_context(ev: Engagement, hs: HubSpot, contact: dict | None, deals: list[dict] | None = None) -> list[dict]:
    deals = deals if deals is not None else _contact_deals(hs, contact)
    company_deals = _company_deals(hs, ev, contact)
    policy.stamp_deal_context(ev, contact, deals, company_deals)
    return deals


def _is_unidentified_cube_phone(ev: Engagement, contact: dict | None) -> bool:
    return policy.is_unidentified_cube_phone(ev, contact)


def _find_hubspot_contact(hs: HubSpot, ev: Engagement) -> dict | None:
    return policy.resolve_engagement_contact(hs, ev)


def _annotate_sales_context(ev: Engagement, settings: Settings, hs: HubSpot, already: dict | None = None) -> dict | None:
    """Set intent flags / already_prospect from one cached classify + open pre-sale deals."""
    if already is None:
        already = _find_hubspot_contact(hs, ev)
    deals = _mark_closed_won_context(ev, hs, already)
    if ev.source not in {"cube_acr", "fireflies"}:
        return already
    decision = intent.classify(settings, ev)
    intent.apply_deal_holder_veto(ev, decision)
    ev.extra["intent_yes"] = policy.cube_has_sales_intent(ev, decision, settings.intent_min_confidence)
    ev.extra["already_prospect"] = policy.contact_is_prospect(already, deals)
    return already


def _handle_budget_kind(ev: Engagement, already: dict | None, hs: HubSpot, settings: Settings | None = None) -> str | None:
    """One create slot per new contact/deal; stage_move for an existing live deal."""
    deals = _mark_closed_won_context(ev, hs, already)
    company_deals = _company_deals(hs, ev, already)
    if policy.closed_won_notes_only(ev, deals, contact=already, company_deals=company_deals):
        return None
    stage = policy.resolve_stage(ev)
    live = policy.live_open_deals(deals)
    creating_contact = already is None and bool(stage) and policy.may_create_hubspot_contact(ev)
    creating_deal = bool(stage) and not live
    if ev.source in {"cube_acr", "fireflies"} and creating_deal and not policy.held_call_may_open_deal(ev):
        creating_deal = False
        creating_contact = False
    if creating_contact or creating_deal:
        return "create"
    if stage and live:
        current = (max(live, key=policy.deal_richness).get("properties") or {}).get("dealstage") or ""
        deal = max(live, key=policy.deal_richness)
        if policy.choose_deal_action(current, stage, ev, deal=deal, settings=settings):
            return "stage_move"
    return None


def _person_key_for(ev: Engagement) -> str:
    from crmbrain.evidence import person_key

    return person_key(ev.email, ev.phone, ev.display_name() or ev.name)


def _event_unprocessed(ev: Engagement, memory: Memory | None) -> bool:
    if memory is None or not hasattr(memory, "already_processed"):
        return True
    return not memory.already_processed(ev.source, ev.external_id)


def should_reextract(settings: Settings, ev: Engagement) -> bool:
    """Rerun deal_terms on already-processed Fireflies/Cube events."""
    since = getattr(settings, "reextract_since", None)
    if since is None:
        return False
    if ev.source not in {"fireflies", "cube_acr"}:
        return False
    if ev.occurred_at and ev.occurred_at < since:
        return False
    return True


def _handle_would_write(ev: Engagement, hs: HubSpot, memory: Memory | None) -> bool:
    if not _event_unprocessed(ev, memory):
        return False
    if ev.source in policy.NEVER_OPEN_DEAL_SOURCES | {"gmail", "gmail_person"}:
        return False
    if ev.source == "cube_acr":
        return policy.is_cube_business_discovery(ev)
    if ev.source == "fireflies":
        return policy.held_call_may_open_deal(ev)
    return True


def _planned_unique_people(
    engagements: list[Engagement],
    settings: Settings,
    hs: HubSpot,
    memory: Memory,
    timelines,
    upcoming: set[str],
    held_events: list[Engagement],
    calendar_api_ok: bool,
) -> int:
    """Unprocessed people who would be written, counted once across handle + reconcile."""
    from crmbrain.reconcile import planned_change_person_keys

    keys = set()
    for ev in engagements:
        if not _handle_would_write(ev, hs, memory):
            continue
        key = _person_key_for(ev)
        if key:
            keys.add(key)
    keys |= planned_change_person_keys(
        hs, settings, timelines, upcoming, held_events, calendar_api_ok, memory
    )
    return len(keys)


def _record_person_intent_no(
    report: CycleReport,
    memory: Memory,
    ev: Engagement,
    decision,
    *,
    already_processed: bool = False,
    mark_processed: bool = True,
) -> None:
    label = ev.display_name() or ev.email or ev.phone or ev.external_id
    line = f"{label} {decision.intent} {decision.reason}".strip()
    if line not in report.review_queue:
        report.review_queue.append(line)
    report.skipped.append(f"{ev.source}:{label} {decision.intent or 'no'}")
    if hasattr(memory, "enqueue_review"):
        memory.enqueue_review(
            {
                "person_key": ev.email or ev.phone or ev.display_name(),
                "email": ev.email,
                "name": ev.display_name(),
                "company": ev.company,
                "intent": decision.intent,
                "confidence": decision.confidence,
                "reason": decision.reason,
                "evidence": {"source": ev.source, "subject": ev.raw_subject},
            }
        )
    if mark_processed and not already_processed and not intent.person_has_booking_or_commerce(ev):
        memory.mark_processed(ev.source, ev.external_id, {"skip": decision.intent or "person_intent_no"})


def _propose_engagement(
    report: CycleReport,
    ev: Engagement,
    decision,
    already: dict | None,
) -> None:
    stage = decision.stage or ev.stage_hint or policy.resolve_stage(ev)
    action = "create" if not already else "update"
    if action == "create" and not stage:
        return
    raw_amount = str(getattr(decision, "amount", "") or "")
    amount = ""
    if raw_amount:
        amount = intelligence.deal_amount_to_write(None, raw_amount, ev=ev) or ""
    propose_deal_write(
        report,
        action=action,
        label=ev.display_name() or ev.email or ev.phone,
        stage=stage,
        amount=amount,
        reason=decision.reason,
        contact_id=str((already or {}).get("id") or ""),
        ev=ev,
    )
    if not intent.is_confident_sales(decision) and not intent.is_confident_non_sales(decision):
        report.review_queue.append(
            f"{ev.display_name() or ev.email} {decision.verdict} {decision.reason}"
        )


def _handle_engagement(
    ev: Engagement,
    settings: Settings,
    hs: HubSpot,
    memory: Memory,
    hey: HeyReach | None,
    report: CycleReport,
    budget: WriteBudget | None = None,
) -> None:
    budget = budget or WriteBudget.from_settings(settings)
    if ev.email and is_josh_address(ev.email):
        report.skipped.append(f"{ev.source}:{ev.email} josh address")
        memory.mark_processed(ev.source, ev.external_id, {"skip": "josh_address"})
        return
    if ev.email and is_junk_crm_email(ev.email):
        report.junk_blocked.append(f"{ev.source}:{ev.email} system address")
        memory.mark_processed(ev.source, ev.external_id, {"skip": "system_email"})
        return
    if is_non_deal_person(
        name=ev.display_name() or ev.name or f"{ev.first_name} {ev.last_name}".strip(),
        email=ev.email,
        company=ev.company,
        phone=ev.phone,
    ):
        report.skipped.append(f"{ev.source}:{ev.display_name() or ev.email} excluded")
        memory.mark_processed(ev.source, ev.external_id, {"skip": "non_deal"})
        return
    if is_personal(name=ev.display_name(), phone=ev.phone, email=ev.email):
        if not policy.personal_allowed_for_sales_intro(ev):
            report.skipped.append(f"{ev.source}:{ev.display_name() or ev.phone} personal")
            memory.mark_processed(ev.source, ev.external_id, {"skip": "personal"})
            return
    if not (ev.email or ev.phone or ev.display_name() or ev.linkedin_url):
        report.junk_blocked.append(f"{ev.source}:{ev.external_id} no identity")
        memory.mark_processed(ev.source, ev.external_id, {"skip": "no_identity"})
        return
    if ev.source == "cube_acr_meta":
        report.skipped.append(f"cube_acr {ev.external_id} audio has no transcript yet")
        memory.mark_processed(ev.source, ev.external_id, {"skip": "no_transcript"})
        return
    already = _find_hubspot_contact(hs, ev)
    _mark_closed_won_context(ev, hs, already)
    if (ev.extra or {}).get("name_ambiguous"):
        line = f"{ev.display_name() or ev.name or ev.phone} ambiguous_name"
        if line not in report.review_queue:
            report.review_queue.append(line)
        report.skipped.append(f"{ev.source}:{ev.display_name() or ev.phone} ambiguous_name")
        memory.mark_processed(ev.source, ev.external_id, {"skip": "ambiguous_name"})
        return
    if is_excluded_contact(ev, already):
        report.skipped.append(f"{ev.source}:{ev.display_name() or ev.email} excluded")
        memory.mark_processed(ev.source, ev.external_id, {"skip": "non_deal"})
        return
    decision = intent.classify(settings, ev)
    intent.apply_deal_holder_veto(ev, decision)
    if is_personal_family_intent(decision.intent):
        report.skipped.append(f"{ev.source}:{ev.display_name() or ev.phone} {decision.intent}")
        memory.mark_processed(ev.source, ev.external_id, {"skip": decision.intent or "personal"})
        return
    if intent.person_blocks_deal(ev, settings):
        _record_person_intent_no(
            report,
            memory,
            ev,
            ev._person_intent,
            mark_processed=not intent.person_has_booking_or_commerce(ev),
        )
        return
    if getattr(ev, "_person_intent", None) and intent.commerce_overrides_person_no(ev, settings):
        _record_person_intent_no(
            report, memory, ev, ev._person_intent, mark_processed=False
        )
    already = _annotate_sales_context(ev, settings, hs, already)
    if _is_unidentified_cube_phone(ev, already):
        line = f"{ev.phone} unknown phone"
        if line not in report.review_queue:
            report.review_queue.append(line)
        if hasattr(memory, "enqueue_review"):
            memory.enqueue_review(
                {
                    "person_key": ev.phone or ev.external_id,
                    "phone": ev.phone,
                    "name": ev.display_name(),
                    "reason": "unknown_phone",
                    "evidence": {"source": ev.source, "subject": ev.raw_subject},
                }
            )
        report.skipped.append(f"{ev.source}:{ev.phone} unknown phone")
        memory.mark_processed(ev.source, ev.external_id, {"skip": "unknown_phone"})
        return
    deals = _mark_closed_won_context(ev, hs, already)
    notes_only = policy.closed_won_notes_only(ev, deals, contact=already, company_deals=_company_deals(hs, ev, already))
    if ev.source in {"cube_acr", "fireflies"}:
        salesish = policy.cube_has_sales_intent(ev, decision, settings.intent_min_confidence)
    elif ev.source in policy.INITIAL_INTEREST_SOURCES:
        salesish = True
    else:
        salesish = intent.is_confident_sales(decision, settings.intent_min_confidence)
    if notes_only and already:
        if settings.dry_run:
            return
        if memory.already_processed(ev.source, ev.external_id):
            _apply_transcript_intelligence(
                ev,
                settings,
                hs,
                memory,
                report,
                already,
                add_timeline_note=False,
                budget=budget,
            )
            report.skipped.append(f"{ev.source}:{ev.external_id} signed/paid notes only")
            return
        _apply_transcript_intelligence(
            ev,
            settings,
            hs,
            memory,
            report,
            already,
            add_timeline_note=True,
            budget=budget,
        )
        memory.mark_processed(ev.source, ev.external_id, {"skip": "closed_won_notes"})
        report.processed.append(f"{ev.source}:{ev.external_id}")
        return
    if ev.source in policy.HUBSPOT_CREATE_SOURCES and intent.is_confident_non_sales(
        decision, settings.intent_min_confidence
    ):
        mark = not intent.person_has_booking_or_commerce(ev)
        if intent.is_confident_no_intent(decision, settings.intent_min_confidence):
            _record_person_intent_no(report, memory, ev, decision, mark_processed=mark)
        else:
            report.skipped.append(f"{ev.source}:{ev.display_name() or ev.email} {decision.intent}")
            if mark:
                memory.mark_processed(ev.source, ev.external_id, {"skip": decision.intent})
        return
    if ev.source in policy.HUBSPOT_CREATE_SOURCES and not salesish and not already:
        report.review_queue.append(
            f"{ev.display_name() or ev.email} {decision.verdict} {decision.reason}"
        )
        if hasattr(memory, "enqueue_review"):
            memory.enqueue_review(
                {
                    "person_key": ev.email or ev.phone or ev.display_name(),
                    "email": ev.email,
                    "name": ev.display_name(),
                    "company": ev.company,
                    "intent": decision.intent,
                    "confidence": decision.confidence,
                    "reason": decision.reason,
                    "evidence": {"source": ev.source, "subject": ev.raw_subject},
                }
            )
        memory.mark_processed(ev.source, ev.external_id, {"skip": "intent_review"})
        return
    meeting_evidence = None
    if already and not policy.may_create_hubspot_contact(ev):
        meeting_evidence = prune.has_live_meeting_evidence(hs, already)
    if not policy.may_write_hubspot(ev, already is not None, meeting_evidence=meeting_evidence):
        if already and meeting_evidence is False and not settings.dry_run:
            prune.archive_unengaged_contact(hs, already, report, "no meeting")
        if memory.already_processed(ev.source, ev.external_id):
            report.skipped.append(f"{ev.source}:{ev.external_id} already processed")
            return
        skip_line = (
            f"{ev.source}:{ev.display_name() or ev.email or ev.phone} no meeting, skip HubSpot"
        )
        if settings.dry_run:
            report.skipped.append(skip_line)
            return
        reason = ev.ticker_reason or facts_reason_for_ticker(ev)
        if policy.should_enroll_ticker_without_hubspot(ev) and reason:
            enrolled = ticker.enroll(memory, ev, reason)
            if enrolled:
                report.ticker_enrolled.append(f"{ev.display_name() or ev.email} {reason}")
        _queue_linkedin(settings, hey, ev, hs, memory, report, contact=None)
        report.skipped.append(skip_line)
        memory.mark_processed(ev.source, ev.external_id, {"skip": "no_meeting_hubspot"})
        report.processed.append(f"{ev.source}:{ev.external_id}")
        return

    if memory.already_processed(ev.source, ev.external_id):
        reextract = should_reextract(settings, ev)
        if settings.dry_run and not reextract:
            report.skipped.append(f"{ev.source}:{ev.external_id} already processed")
            return
        if ev.source in {"fireflies", "cube_acr"} and already:
            if settings.dry_run and reextract:
                facts = intelligence.extract(settings, ev)
                requested = policy.resolve_stage(ev, facts)
                amount = facts.get("amount_hint") or facts.get("deal_amount") or ""
                if isinstance(facts.get("deal_terms"), dict):
                    ev.extra = dict(ev.extra or {})
                    ev.extra.setdefault("deal_terms", facts["deal_terms"])
                deals = _mark_closed_won_context(ev, hs, already)
                if policy.closed_won_notes_only(
                    ev, deals, contact=already, company_deals=_company_deals(hs, ev, already)
                ):
                    report.skipped.append(f"{ev.source}:{ev.external_id} reextract notes only")
                    return
                live = policy.live_open_deals(deals)
                deal_row = max(live, key=policy.deal_richness) if live else None
                current_stage = str((deal_row.get("properties") or {}).get("dealstage") or "") if deal_row else ""
                current_amount = str((deal_row.get("properties") or {}).get("amount") or "") if deal_row else ""
                write_stage, write_amount, write_reason = authorize_deal_write(
                    ev,
                    requested_stage=requested,
                    amount=str(amount or ""),
                    contact=already,
                    deal=deal_row,
                    deals=deals,
                    company_deals=_company_deals(hs, ev, already),
                    settings=settings,
                )
                if not write_stage and not write_amount:
                    report.skipped.append(
                        f"{ev.source}:{ev.external_id} reextract no-op current={current_stage}/{current_amount}"
                    )
                    return
                kind = None
                if write_stage and not live:
                    kind = "create"
                elif write_stage and live:
                    kind = "stage_move"
                if kind and not _reserve_budget(budget, kind, memory, report, ev):
                    return
                if write_amount and not _reserve_budget(budget, "amount", memory, report, ev):
                    return
                reason = f"reextract current={current_stage}/{current_amount} {write_reason}"
                propose_deal_write(
                    report,
                    action="update",
                    label=ev.display_name() or ev.email or ev.phone,
                    stage=write_stage or current_stage,
                    amount=str(write_amount or ""),
                    contact_id=str(already.get("id") or ""),
                    deal_id=str((deal_row or {}).get("id") or ""),
                    reason=reason,
                    ev=ev,
                )
                logger.info(
                    "reextract preview %s stage %s amount %s (%s)",
                    ev.display_name() or ev.email or ev.phone,
                    write_stage or current_stage,
                    write_amount or current_amount,
                    reason,
                )
                report.skipped.append(f"{ev.source}:{ev.external_id} reextract dry-run")
                return
            _apply_transcript_intelligence(
                ev,
                settings,
                hs,
                memory,
                report,
                already,
                add_timeline_note=False,
                budget=budget,
            )
            report.skipped.append(f"{ev.source}:{ev.external_id} refreshed notes/amount")
        else:
            report.skipped.append(f"{ev.source}:{ev.external_id} already processed")
        return

    if settings.dry_run:
        kind = _handle_budget_kind(ev, already, hs, settings)
        if not kind:
            report.skipped.append(
                f"{ev.source}:{ev.display_name() or ev.email or ev.phone} no hubspot write"
            )
            return
        if kind == "create":
            ok, reason = policy.may_open_new_deal(
                ev, already, _contact_deals(hs, already), settings, _company_deals(hs, ev, already)
            )
            if not ok:
                report.skipped.append(f"{ev.source}:{ev.display_name() or ev.phone} {reason}")
                if reason in {"unknown_phone", "ambiguous_name", "non_person"}:
                    if reason == "unknown_phone":
                        line = f"{ev.phone} unknown phone"
                    else:
                        line = f"{ev.display_name() or ev.name or ev.phone} {reason}"
                    if line not in report.review_queue:
                        report.review_queue.append(line)
                return
        if not _reserve_budget(budget, kind, memory, report, ev):
            return
        _propose_engagement(report, ev, decision, already)
        return

    kind = _handle_budget_kind(ev, already, hs, settings)
    if not _reserve_budget(budget, kind, memory, report, ev):
        return
    if kind == "create" or not already:
        ok, reason = policy.may_open_new_deal(
            ev, already, _contact_deals(hs, already), settings, _company_deals(hs, ev, already)
        )
        if not ok:
            report.skipped.append(f"{ev.source}:{ev.display_name() or ev.phone} {reason}")
            if reason in {"unknown_phone", "ambiguous_name", "non_person"}:
                if reason == "unknown_phone":
                    line = f"{ev.phone} unknown phone"
                else:
                    line = f"{ev.display_name() or ev.name or ev.phone} {reason}"
                if line not in report.review_queue:
                    report.review_queue.append(line)
            memory.mark_processed(ev.source, ev.external_id, {"skip": reason})
            return

    ev = enrichment.enrich(settings, ev)
    contact = hs.upsert_contact(ev)
    report.contacts_upserted.append(f"{ev.display_name() or ev.email} ({ev.source})")
    base = already or hs.find_contact(email=ev.email, phone=ev.phone, name=ev.display_name()) or contact
    base["id"] = contact["id"]
    facts = _apply_transcript_intelligence(
        ev,
        settings,
        hs,
        memory,
        report,
        base,
        add_timeline_note=True,
        budget=budget,
        reserved=kind,
    )

    reason = facts.get("ticker_reason") or ev.ticker_reason
    if ev.source == "smartlead" and not reason:
        reason = "never_booked"
    if reason == "no_show" and policy.is_meeting_held(ev):
        reason = ""
    if reason:
        enrolled = ticker.enroll(memory, ev, reason, hs_contact_id=contact["id"])
        if enrolled:
            report.ticker_enrolled.append(f"{ev.display_name()} {reason}")

    _queue_linkedin(settings, hey, ev, hs, memory, report, contact=contact)

    memory.mark_processed(ev.source, ev.external_id, {"contact_id": contact["id"]})
    report.processed.append(f"{ev.source}:{ev.external_id}")


def _apply_transcript_intelligence(
    ev: Engagement,
    settings: Settings,
    hs: HubSpot,
    memory: Memory,
    report: CycleReport,
    contact: dict,
    *,
    add_timeline_note: bool,
    budget: WriteBudget | None = None,
    reserved: str | None = None,
) -> dict:
    """Always extract → merge_contact_props for meeting transcripts. Fill deal amount if empty."""
    facts = intelligence.extract(settings, ev)
    merged = intelligence.merge_contact_props(contact, facts)
    if merged:
        try:
            hs.patch_contact(contact["id"], merged, ev=ev, contact=contact)
            report.notes_updated.append(f"{ev.display_name() or ev.email} ({ev.source})")
            props = contact.setdefault("properties", {})
            props.update(merged)
            logger.info("notes_updated %s %s", ev.display_name() or ev.email, sorted(merged))
        except Exception as exc:
            report.errors.append(f"notes {ev.display_name() or ev.email}: {exc}")
            logger.warning("notes patch failed %s: %s", ev.display_name() or ev.email, exc)
    if add_timeline_note:
        note = ev.summary or ev.transcript[:1500] or ev.raw_subject
        if note:
            try:
                hs.add_note(
                    contact["id"],
                    f"{ev.source} {ev.occurred_at or ''}\n\n{note}",
                    ev=ev,
                    contact=contact,
                )
            except Exception as exc:
                report.errors.append(f"timeline note {ev.display_name() or ev.email}: {exc}")
    for fact_type in ("personal_details", "family_notes", "relationship_hooks"):
        if facts.get(fact_type):
            memory.save_fact(
                {
                    "hs_contact_id": contact["id"],
                    "fact_type": fact_type,
                    "source": ev.source,
                    "fact_text": facts[fact_type],
                    "external_id": ev.external_id,
                }
            )

    stage = policy.resolve_stage(ev, facts)
    if policy.unclear_nurture(ev, facts):
        deals_flag = _contact_deals(hs, contact)
        live_flag = policy.live_open_deals(deals_flag)
        if live_flag and not settings.dry_run:
            from crmbrain.hubspot import set_josh_review_flag

            set_josh_review_flag(
                hs, max(live_flag, key=policy.deal_richness), "unclear_nurture"
            )
        report.review_queue.append(f"{ev.display_name() or ev.email} unclear_nurture")
    if policy.has_poc_evidence(ev) and stage not in {STAGE["signed"], STAGE["paid"]}:
        line = f"{ev.display_name() or ev.email} poc_hint"
        if line not in report.review_queue:
            report.review_queue.append(line)
        if hasattr(memory, "enqueue_review"):
            memory.enqueue_review(
                {
                    "person_key": ev.email or ev.phone or ev.display_name(),
                    "email": ev.email,
                    "name": ev.display_name(),
                    "company": ev.company,
                    "reason": "poc_hint",
                    "evidence": {"source": ev.source, "subject": ev.raw_subject},
                }
            )
    if not stage and policy.is_client_context_ev(ev):
        report.skipped.append(f"{ev.display_name()} client conversation, notes only")
    deals = _mark_closed_won_context(ev, hs, contact)
    if policy.closed_won_notes_only(ev, deals, contact=contact, company_deals=_company_deals(hs, ev, contact)):
        if policy.has_paperwork_evidence(ev):
            line = f"{ev.display_name() or ev.email} paperwork"
            if line not in report.review_queue:
                report.review_queue.append(line)
            if hasattr(memory, "enqueue_review"):
                memory.enqueue_review(
                    {
                        "person_key": ev.email or ev.phone or ev.display_name(),
                        "email": ev.email,
                        "name": ev.display_name(),
                        "company": ev.company,
                        "reason": "paperwork",
                        "evidence": {"source": ev.source, "subject": ev.raw_subject},
                    }
                )
        report.skipped.append(f"{ev.display_name() or ev.email} signed/paid notes only")
        return facts
    amount = facts.get("amount_hint") or facts.get("deal_amount") or ""
    if isinstance(facts.get("deal_terms"), dict):
        ev.extra = dict(ev.extra or {})
        ev.extra.setdefault("deal_terms", facts["deal_terms"])
        ev.extra.setdefault("amount_source", intelligence.amount_source_kind(ev))
    if stage or amount:
        live = policy.live_open_deals(deals)
        if ev.source in {"cube_acr", "fireflies"} and not live and not policy.held_call_may_open_deal(ev):
            return facts
        if policy.requires_josh_meeting_to_open_deal(ev) and not live:
            return facts
        prev_amount = ""
        prev_stage = ""
        deal_row = None
        if live:
            deal_row = max(live, key=policy.deal_richness)
            prev_amount = str((deal_row.get("properties") or {}).get("amount") or "")
            prev_stage = str((deal_row.get("properties") or {}).get("dealstage") or "")
        stage_out, amount_out, write_reason = authorize_deal_write(
            ev,
            requested_stage=stage,
            amount=amount,
            contact=contact,
            deal=deal_row,
            deals=deals,
            company_deals=_company_deals(hs, ev, contact),
            settings=settings,
        )
        if not stage_out and not amount_out:
            if write_reason == "refresh" and prev_stage:
                try:
                    commit_deal_write(hs, contact, ev, prev_stage)
                except Exception as exc:
                    report.errors.append(f"deal {ev.display_name() or ev.email}: {exc}")
                    logger.warning("deal refresh failed %s: %s", ev.display_name() or ev.email, exc)
            return facts
        if reserved is None:
            extra_kind = None
            if write_reason == "create":
                extra_kind = "create"
            elif write_reason == "move":
                extra_kind = "stage_move"
            if extra_kind and not _reserve_budget(budget, extra_kind, memory, report, ev):
                return facts
        if amount_out and not _reserve_budget(budget, "amount", memory, report, ev):
            return facts
        try:
            deal = commit_deal_write(hs, contact, ev, stage_out or prev_stage, amount=amount_out)
        except Exception as exc:
            report.errors.append(f"deal {ev.display_name() or ev.email}: {exc}")
            logger.warning("deal write failed %s: %s", ev.display_name() or ev.email, exc)
            return facts
        written_stage = str((deal.get("properties") or {}).get("dealstage") or "")
        if deal.get("id") and stage_out and written_stage == stage_out and written_stage != prev_stage:
            report.deals_moved.append(f"{ev.display_name()} -> {stage_out} ({deal.get('id')})")
            if stage_out in {STAGE["discovery_scheduled"], STAGE["discovery_completed"], STAGE["paid"], STAGE["signed"]}:
                memory.stop_ticker(email=ev.email, hs_contact_id=contact["id"])
        live_amount = (deal.get("properties") or {}).get("amount")
        wrote_amount = False
        if deal.get("id") and amount_out:
            if not intelligence.amounts_equal(prev_amount, amount_out) and intelligence.amounts_equal(live_amount, amount_out):
                wrote_amount = True
            elif not intelligence.amounts_equal(live_amount, amount_out):
                wrote_amount = commit_amount_write(hs, deal, amount_out, ev=ev, contact=contact)
        if wrote_amount:
            report.amounts_set.append(f"{ev.display_name() or ev.email} {amount_out}")
            logger.info("amounts_set %s %s", ev.display_name() or ev.email, amount_out)
    return facts


def facts_reason_for_ticker(ev: Engagement) -> str:
    if ev.source == "smartlead":
        return ev.ticker_reason or "never_booked"
    if ev.source in {"heyreach", "rvm"}:
        return ev.ticker_reason or "never_booked"
    return ev.ticker_reason or ""


def _has_meeting_or_open_deal(hs: HubSpot, ev: Engagement, contact: dict | None) -> bool:
    """Booked/held meetings and open deals never go to HeyReach outreach."""
    extra = ev.extra or {}
    if extra.get("create_new") or extra.get("gcal_create"):
        return True
    if ev.source in {"calendly", "fireflies", "cube_acr"}:
        return True
    if policy.is_meeting_held(ev) or policy.is_meeting_scheduled(ev):
        return True
    email = (ev.email or "").strip().lower()
    upcoming = {e.lower() for e in (getattr(hs, "scheduled_attendee_emails", None) or set())}
    recent = {e.lower() for e in (getattr(hs, "recent_attendee_emails", None) or set())}
    if email and (email in upcoming or email in recent):
        return True
    if contact and contact.get("id"):
        if policy.live_open_deals(_contact_deals(hs, contact)):
            return True
        cid = str(contact["id"])
        if hasattr(hs, "contact_has_meetings"):
            try:
                if hs.contact_has_meetings(cid):
                    return True
            except Exception:
                pass
        if hasattr(hs, "contact_has_future_meetings"):
            try:
                if hs.contact_has_future_meetings(cid):
                    return True
            except Exception:
                pass
    return False


def _heyreach_id(ev: Engagement) -> str:
    if ev.email:
        return ev.email.lower()
    if usable_linkedin(ev.linkedin_url):
        return usable_linkedin(ev.linkedin_url).lower()
    return (ev.display_name() or "").lower()


def _queue_linkedin(
    settings: Settings,
    hey: HeyReach | None,
    ev: Engagement,
    hs: HubSpot,
    memory: Memory,
    report: CycleReport,
    contact: dict | None = None,
) -> None:
    """Anyone Josh called, emailed, or talked to on LinkedIn gets a HeyReach invite."""
    if settings.dry_run or not hey or ev.source == "heyreach":
        return
    if is_excluded_contact(ev, contact):
        return
    if is_personal(name=ev.display_name(), phone=ev.phone, email=ev.email):
        return
    if ev.email and (is_josh_address(ev.email) or should_skip_email(ev.email)):
        return
    if _has_meeting_or_open_deal(hs, ev, contact):
        report.skipped.append(
            f"heyreach {ev.display_name() or ev.email} meeting_or_deal"
        )
        return
    hid = _heyreach_id(ev)
    if not hid or memory.already_processed("heyreach", hid):
        return
    if contact:
        props = contact.get("properties") or {}
        ev.linkedin_url = ev.linkedin_url or props.get("hs_linkedin_url") or ""
        ev.email = ev.email or props.get("email") or ""
        ev.company = ev.company or props.get("company") or ""
        ev.title = ev.title or props.get("jobtitle") or ev.title
        ev.first_name = ev.first_name or props.get("firstname") or ""
        ev.last_name = ev.last_name or props.get("lastname") or ""
    ev.linkedin_url = usable_linkedin(ev.linkedin_url)
    if not ev.linkedin_url:
        ev = enrichment.fill_linkedin(settings, ev)
    try:
        status = hey.add_lead(ev)
    except Exception as exc:
        report.errors.append(f"heyreach {ev.display_name() or ev.email}: {exc}")
        return
    if status != "queued":
        report.skipped.append(f"heyreach {ev.display_name() or ev.email} {status}")
        return
    memory.mark_processed("heyreach", hid, {"linkedin": ev.linkedin_url, "email": ev.email})
    if ev.linkedin_url and contact and contact.get("id"):
        try:
            hs.patch_contact(
                contact["id"], {"hs_linkedin_url": ev.linkedin_url}, ev=ev, contact=contact
            )
        except Exception:
            pass
    report.linkedin_queued.append(ev.display_name() or ev.email)


def _backfill_hubspot_invites(
    settings: Settings,
    hs: HubSpot,
    hey: HeyReach,
    memory: Memory,
    report: CycleReport,
    limit: int = 25,
) -> None:
    """HubSpot is engaged people. Queue anyone not already sent to HeyReach.

    `iter_contacts` retries HubSpot read timeouts so a one-off 30s stall does
    not mark the cycle partial when the retry succeeds.
    """
    queued = 0
    for row in hs.iter_contacts(
        ["email", "firstname", "lastname", "phone", "company", "jobtitle", "hs_linkedin_url"]
    ):
        if queued >= limit:
            break
        props = row.get("properties") or {}
        ev = Engagement(
            source="hubspot_backfill",
            external_id=str(row.get("id") or ""),
            email=props.get("email") or "",
            first_name=props.get("firstname") or "",
            last_name=props.get("lastname") or "",
            phone=props.get("phone") or "",
            company=props.get("company") or "",
            title=props.get("jobtitle") or "",
            linkedin_url=props.get("hs_linkedin_url") or "",
        )
        if is_excluded_contact(ev, row):
            report.skipped.append(f"hubspot_backfill:{ev.display_name() or ev.email} excluded")
            continue
        before = len(report.linkedin_queued)
        _queue_linkedin(settings, hey, ev, hs, memory, report, contact=row)
        if len(report.linkedin_queued) > before:
            queued += 1


def integration_status(settings: Settings) -> list[str]:
    """Present/missing only. Never include secret values."""
    gmail = bool(
        settings.gmail_client_id and settings.gmail_client_secret and settings.gmail_refresh_token
    )
    checks = (
        ("HubSpot", bool(settings.hubspot_token)),
        ("Gmail", gmail),
        ("Fireflies", bool(settings.fireflies_key)),
        ("Smartlead", bool(settings.smartlead_key)),
        ("HeyReach key", bool(settings.heyreach_key)),
        ("Slack token", bool(settings.slack_token)),
        ("Supabase key", bool(settings.supabase_key)),
        ("Cube ACR", bool(settings.cube_folder) and has_drive_access(settings)),
    )
    return [f"{name}: {'present' if ok else 'missing'}" for name, ok in checks]


def _recovered_rate_limit_note(err: str) -> bool:
    """True only for notes about 429/503 that later succeeded — not skipped data."""
    lower = err.lower()
    if "429" not in lower and "too many requests" not in lower and "503" not in lower:
        return False
    return any(
        token in lower
        for token in ("recovered", "succeeded after", "retried ok", "retry succeeded")
    )


def _is_hubspot_429_error(err: str) -> bool:
    lower = (err or "").lower()
    if "429" not in lower and "too many requests" not in lower:
        return False
    return "hubspot" in lower or "hubapi" in lower or "api.hubapi" in lower


def _is_calendar_auth_error(msg: str) -> bool:
    low = (msg or "").lower()
    return "401" in low or "403" in low or "permissionerror" in low or "calendar api 401" in low or "calendar api 403" in low


def _stale_source_warning(report: CycleReport, source: str, detail: str) -> None:
    line = f"{source}: {detail}"
    if line in report.warnings or any(w.startswith(f"{source}:") for w in report.warnings):
        return
    report.warnings.append(line)
    if not any(s.startswith(source) for s in report.stale_sources):
        report.stale_sources.append(f"{source} stale-source warning ({detail})")


def _finish_status(settings: Settings, report: CycleReport) -> str:
    if settings.dry_run:
        return "dry_run"
    return cycle_status(report)


def cycle_status(report: CycleReport) -> str:
    """ok unless data was skipped after exhausted retries or another hard error.

    Transient Smartlead 429/503 that later succeeded must not flip the cycle to
    partial. Those belong in logs, not report.errors; recovered notes are ignored.
    HubSpot search 429s are retried with jitter; leftover 429s must not crash Railway.
    """
    for err in report.errors:
        if _recovered_rate_limit_note(err):
            continue
        if _is_hubspot_429_error(err):
            continue
        return "partial"
    return "ok"


def process_exit_code(report: CycleReport) -> int:
    """Railway treats exit 1 as CRASHED. Dry-run and HubSpot-429-only runs are 0."""
    if report.dry_run:
        return 0
    return 0 if cycle_status(report) == "ok" else 1


def _flush_memory_errors(memory: Memory, report: CycleReport) -> None:
    for msg in memory.drain_errors():
        if msg not in report.errors:
            report.errors.append(msg)


def _fire_ticker(settings: Settings, memory: Memory, report: CycleReport, *, now=None) -> None:
    from crmbrain.nurture import fire_due_rows

    fire_due_rows(settings, memory, report, now=now)


def _mail_contact(hs: HubSpot, ev: Engagement) -> dict | None:
    contact_id = (ev.extra or {}).get("hubspot_contact_id") or ""
    found = None
    if ev.email or ev.phone or ev.display_name():
        try:
            found = hs.find_contact(email=ev.email, phone=ev.phone, name=ev.display_name())
        except Exception:
            found = None
    if found:
        return found
    if contact_id:
        return {"id": contact_id, "properties": {}}
    return None


def _contact_deal_context(hs: HubSpot, contact: dict | None) -> tuple[str, bool]:
    """Return (current live stage, has closed-won deal)."""
    if not contact or not contact.get("id"):
        return "", False
    try:
        existing = hs.open_deals_for_contact(contact["id"])
    except Exception:
        return "", False
    live = policy.live_open_deals(existing)
    current = ""
    if live:
        deal = max(live, key=policy.deal_richness)
        current = (deal.get("properties") or {}).get("dealstage") or ""
    return current, policy.has_closed_won_deal(existing)


def _has_reschedule(hs: HubSpot, ev: Engagement) -> bool:
    email = (ev.email or "").strip().lower()
    if not email:
        return False
    upcoming = {e.lower() for e in (getattr(hs, "scheduled_attendee_emails", None) or set())}
    return email in upcoming


def apply_gmail_stage_update(
    ev: Engagement,
    settings: Settings,
    hs: HubSpot,
    memory: Memory,
    hey: HeyReach | None,
    report: CycleReport,
    *,
    held_events: list[Engagement] | None = None,
    budget: WriteBudget | None = None,
) -> None:
    budget = budget or WriteBudget.from_settings(settings)
    """Apply a Gmail calendar/billing signal. Re-check held meetings before No Show."""
    already = memory.already_processed(ev.source, ev.external_id)
    if ev.email and is_josh_address(ev.email):
        report.skipped.append(f"{ev.source}:{ev.email} josh address")
        if not already:
            memory.mark_processed(ev.source, ev.external_id, {"skip": "josh_address"})
        return
    if is_non_deal_person(
        name=ev.display_name() or ev.name or f"{ev.first_name} {ev.last_name}".strip(),
        email=ev.email,
        company=ev.company,
        phone=ev.phone,
    ):
        report.skipped.append(f"{ev.source}:{ev.display_name() or ev.email} excluded")
        if not already:
            memory.mark_processed(ev.source, ev.external_id, {"skip": "non_deal"})
        return
    if not getattr(ev, "_person_intent", None):
        intent.attach_person_intent(settings, [ev] + list(held_events or []))
    if intent.person_blocks_deal(ev, settings):
        _record_person_intent_no(
            report,
            memory,
            ev,
            ev._person_intent,
            already_processed=already,
            mark_processed=not intent.person_has_booking_or_commerce(ev),
        )
        return
    if getattr(ev, "_person_intent", None) and intent.commerce_overrides_person_no(ev, settings):
        _record_person_intent_no(
            report, memory, ev, ev._person_intent, already_processed=already, mark_processed=False
        )
    contact = _mail_contact(hs, ev)
    contact_props = (contact or {}).get("properties") or {}
    if is_non_deal_person(
        name=f"{contact_props.get('firstname') or ''} {contact_props.get('lastname') or ''}".strip()
        or ev.display_name(),
        email=contact_props.get("email") or ev.email,
        company=contact_props.get("company") or ev.company,
        phone=contact_props.get("phone") or ev.phone,
    ):
        report.skipped.append(f"{ev.source}:{ev.display_name() or ev.email} excluded")
        if not already:
            memory.mark_processed(ev.source, ev.external_id, {"skip": "non_deal"})
        return
    contact_id = (contact or {}).get("id") or (ev.extra or {}).get("hubspot_contact_id") or ""
    if contact_id:
        from crmbrain import renewals

        deals_ren = _contact_deals(hs, contact)
        moved = renewals.maybe_schedule_renewal_call(hs, deals_ren, ev)
        if moved:
            report.deals_moved.append(
                f"{ev.email or ev.display_name()} renewal -> call_scheduled ({moved.get('id')})"
            )
            if not already:
                memory.mark_processed(ev.source, ev.external_id, {"skip": "renewal_call"})
                report.processed.append(f"gmail:{ev.raw_subject[:60]}")
            return
    if ev.stage_hint in {NO_SHOW_HINT, "no_show"}:
        scheduled_at = policy.scheduled_at_from_engagement(ev)
        if scheduled_at is None:
            scheduled_at = gmail_scan.parse_meeting_at(ev.raw_subject, ev.summary)
        current_stage, has_closed_won = _contact_deal_context(hs, contact)
        write_stage = policy.no_show_write_stage(
            prospect=ev,
            contact=contact,
            current_stage=current_stage,
            held_events=held_events,
            scheduled_at=scheduled_at,
            has_reschedule=_has_reschedule(hs, ev),
            already_processed=already,
            has_closed_won=has_closed_won,
        )
        ev.stage_hint = write_stage
        if write_stage == STAGE["discovery_completed"]:
            extra = dict(ev.extra or {})
            extra["held_meeting"] = True
            extra["meeting_held"] = True
            ev.extra = extra
        if write_stage == INCREMENT_NO_SHOW:
            deals_inc = _contact_deals(hs, contact)
            live_inc = policy.live_open_deals(deals_inc)
            deal_inc = max(live_inc, key=policy.deal_richness) if live_inc else None
            if deal_inc and deal_inc.get("id") and not policy.deal_is_locked(deal_inc):
                from crmbrain.hubspot import increment_no_show_count

                if not settings.dry_run:
                    increment_no_show_count(hs, deal_inc)
                report.deals_moved.append(
                    f"{ev.email or ev.display_name()} no_show_count++ (stage unchanged)"
                )
                enrolled = ticker.enroll(memory, ev, "no_show", hs_contact_id=contact_id)
                if enrolled:
                    report.ticker_enrolled.append(f"{ev.email} no_show")
            else:
                report.skipped.append(f"{ev.email or ev.external_id} no_show no open deal")
            if not already:
                memory.mark_processed(
                    ev.source, ev.external_id, {"subject": ev.raw_subject, "skip": "no_show_count"}
                )
                report.processed.append(f"gmail:{ev.raw_subject[:60]}")
            return
        if already and not write_stage:
            report.skipped.append(f"{ev.source}:{ev.external_id} stale no_show")
            return
        if already and write_stage == STAGE["discovery_completed"] and contact_id:
            deals_ns = _contact_deals(hs, contact)
            live_ns = policy.live_open_deals(deals_ns)
            deal_ns = max(live_ns, key=policy.deal_richness) if live_ns else None
            held = policy.matching_held_event(ev, contact, held_events, scheduled_at)
            gate_ev = held if held else ev
            stage_out, _amt, reason = authorize_deal_write(
                gate_ev,
                requested_stage=write_stage,
                contact=contact,
                deal=deal_ns,
                deals=deals_ns,
                company_deals=_company_deals(hs, ev, contact),
                settings=settings,
            )
            if not stage_out:
                report.skipped.append(f"{ev.email or ev.display_name()} held_beats_noshow {reason}")
                return
            promote_kind = "create" if deal_ns is None else "stage_move"
            if settings.dry_run:
                if not _reserve_budget(budget, promote_kind, memory, report, ev):
                    return
                propose_deal_write(
                    report,
                    action="move" if deal_ns else "create",
                    label=ev.email or ev.display_name(),
                    stage=stage_out,
                    contact_id=str(contact_id),
                    deal_id=str((deal_ns or {}).get("id") or ""),
                    reason="held_beats_noshow",
                    ev=gate_ev,
                )
                return
            if not _reserve_budget(budget, promote_kind, memory, report, ev):
                return
            deal = commit_deal_write(
                hs, contact or {"id": contact_id, "properties": {}}, gate_ev, stage_out
            )
            if deal.get("id"):
                report.deals_moved.append(f"{ev.email} gmail -> {stage_out} ({deal.get('id')})")
            memory.stop_ticker(email=ev.email, hs_contact_id=contact_id)
            return
        if not write_stage:
            report.skipped.append(f"{ev.email or ev.external_id} no_show blocked")
            if not already:
                memory.mark_processed(ev.source, ev.external_id, {"subject": ev.raw_subject, "skip": "no_show_blocked"})
                report.processed.append(f"gmail:{ev.raw_subject[:60]}")
            return
    elif already:
        return

    if ev.extra.get("create_new"):
        ev.source = "calendly"
        _handle_engagement(ev, settings, hs, memory, hey, report, budget=budget)
        return
    deals_pre = _mark_closed_won_context(ev, hs, contact)
    if policy.closed_won_notes_only(
        ev, deals_pre, contact=contact, company_deals=_company_deals(hs, ev, contact)
    ) and not policy.is_payment_event(ev):
        report.skipped.append(
            f"{ev.email or ev.display_name() or ev.external_id} signed/paid notes only"
        )
        if not already:
            memory.mark_processed(ev.source, ev.external_id, {"skip": "closed_won_notes"})
            report.processed.append(f"gmail:{ev.raw_subject[:60]}")
        return
    extra = ev.extra or {}
    if extra.get("josh_sent_proposal") and not policy.is_new_completed_paperwork(ev):
        live_pre = policy.live_open_deals(deals_pre)
        held = policy.matching_held_event(ev, contact, held_events)
        if not live_pre and not held:
            report.skipped.append(
                f"{ev.email or ev.display_name() or ev.external_id} proposal needs open deal or held meeting"
            )
            if not already:
                memory.mark_processed(ev.source, ev.external_id, {"skip": "proposal_no_open_deal"})
                report.processed.append(f"gmail:{ev.raw_subject[:60]}")
            return
    if ev.stage_hint and contact_id:
        deals = _contact_deals(hs, contact)
        live = policy.live_open_deals(deals)
        current = ""
        deal_row = None
        if live:
            deal_row = max(live, key=policy.deal_richness)
            current = (deal_row.get("properties") or {}).get("dealstage") or ""
        elif ev.stage_hint == STAGE["paid"]:
            won = policy.closed_won_deals(deals)
            if won:
                deal_row = max(won, key=policy.deal_richness)
                current = (deal_row.get("properties") or {}).get("dealstage") or ""
        amount = str((ev.extra or {}).get("amount") or "")
        write_stage, write_amount, write_reason = authorize_deal_write(
            ev,
            requested_stage=ev.stage_hint,
            amount=amount,
            contact=contact,
            deal=deal_row,
            deals=deals,
            company_deals=_company_deals(hs, ev, contact),
            settings=settings,
        )
        if not write_stage and not write_amount:
            report.skipped.append(
                f"{ev.email or ev.display_name() or ev.external_id} gmail stage blocked"
            )
            if not already:
                memory.mark_processed(ev.source, ev.external_id, {"skip": "gmail_stage_blocked"})
                report.processed.append(f"gmail:{ev.raw_subject[:60]}")
            return
        if write_stage:
            ev.stage_hint = write_stage
        gmail_kind = "create" if write_reason == "create" else "stage_move" if write_stage else None
        if settings.dry_run:
            if gmail_kind and not _reserve_budget(budget, gmail_kind, memory, report, ev):
                return
            if write_amount and not _reserve_budget(budget, "amount", memory, report, ev):
                return
            propose_deal_write(
                report,
                action=write_reason or "move",
                label=ev.email or ev.display_name(),
                stage=write_stage or current,
                amount=str(write_amount or ""),
                contact_id=str(contact_id),
                deal_id=str((deal_row or {}).get("id") or ""),
                reason="gmail",
                ev=ev,
            )
            return
        if gmail_kind and not _reserve_budget(budget, gmail_kind, memory, report, ev):
            return
        prev_amount = str((deal_row.get("properties") or {}).get("amount") or "") if deal_row else ""
        if write_amount and not _reserve_budget(budget, "amount", memory, report, ev):
            return
        deal = commit_deal_write(
            hs, contact or {"id": contact_id, "properties": {}}, ev, write_stage or current, amount=write_amount
        )
        report.deals_moved.append(f"{ev.email} gmail -> {ev.stage_hint} ({deal.get('id')})")
        live_amount = (deal.get("properties") or {}).get("amount")
        wrote_amount = bool(
            deal.get("id")
            and write_amount
            and not intelligence.amounts_equal(prev_amount, write_amount)
            and intelligence.amounts_equal(live_amount, write_amount)
        )
        if deal.get("id") and write_amount and not wrote_amount:
            wrote_amount = commit_amount_write(hs, deal, write_amount, ev=ev, contact=contact)
        if wrote_amount:
            report.amounts_set.append(f"{ev.email} {write_amount}")
        if ev.stage_hint in {NO_SHOW_HINT, "no_show"}:
            enrolled = ticker.enroll(memory, ev, "no_show", hs_contact_id=contact_id)
            if enrolled:
                report.ticker_enrolled.append(f"{ev.email} no_show")
        if ev.stage_hint in {
            STAGE["paid"],
            STAGE["signed"],
            STAGE["discovery_scheduled"],
            STAGE["discovery_completed"],
        }:
            memory.stop_ticker(email=ev.email, hs_contact_id=contact_id)
    if contact_id:
        try:
            found = contact or (hs.find_contact(email=ev.email) if ev.email else {"id": contact_id})
        except Exception:
            found = {"id": contact_id}
        _queue_linkedin(settings, hey, ev, hs, memory, report, contact=found)
    hold_processed = (
        getattr(ev, "_person_intent", None)
        and intent.is_confident_no_intent(ev._person_intent, settings.intent_min_confidence)
        and intent.person_has_booking_or_commerce(ev)
    )
    if not hold_processed:
        memory.mark_processed(ev.source, ev.external_id, {"subject": ev.raw_subject})
        report.processed.append(f"gmail:{ev.raw_subject[:60]}")


def run(settings: Settings | None = None, briefs_only: bool = False) -> CycleReport:
    settings = settings or Settings.from_env()
    report = CycleReport()
    report.dry_run = bool(settings.dry_run)
    report.integrations.extend(integration_status(settings))
    memory = Memory(settings)
    run_id = memory.start_run()
    _flush_memory_errors(memory, report)
    if not settings.hubspot_token:
        report.errors.append("HUBSPOT_ACCESS_TOKEN missing")
        return report

    last_started = memory.last_finished_run_started_at()
    lookback = compute_lookback_start(settings, last_started)
    reextract_since = getattr(settings, "reextract_since", None)
    if reextract_since is not None:
        if reextract_since.tzinfo is None:
            reextract_since = reextract_since.replace(tzinfo=lookback.tzinfo)
        lookback = min(lookback, reextract_since)
        settings = replace(settings, lookback_start_at=lookback, lookback_override=True, reextract_since=reextract_since)
    else:
        settings = replace(settings, lookback_start_at=lookback)
    budget = WriteBudget.from_settings(settings)
    hs = HubSpot(settings)
    if not briefs_only and not settings.dry_run:
        hs.ensure_properties()
    gmail = Gmail(settings) if settings.gmail_refresh_token else None
    calendar_creates: list[Engagement] = []
    calendar_last: datetime | None = None
    calendar_error = ""
    if gmail and not briefs_only:
        try:
            snap = calendar_events.load_calendar(gmail, settings)
            hs.scheduled_attendee_emails = snap.upcoming
            hs.recent_attendee_emails = snap.recent
            calendar_creates = list(snap.create_engagements)
            if snap.events:
                starts = [e.start for e in snap.events if e.start]
                calendar_last = max(starts) if starts else now_utc()
            report.calendar_api_ok = snap.calendar_api_ok
            if not snap.calendar_api_ok:
                calendar_error = snap.calendar_api_error or "calendar api unavailable"
                if _is_calendar_auth_error(calendar_error):
                    _stale_source_warning(report, "calendar", calendar_error)
                else:
                    report.errors.append(f"calendar: {calendar_error}")
        except Exception as exc:
            calendar_error = str(exc)
            logger.warning("calendar attendees unavailable: %s", exc)
            report.calendar_api_ok = False
            if _is_calendar_auth_error(str(exc)):
                _stale_source_warning(report, "calendar", str(exc))
            else:
                report.errors.append(f"calendar: {exc}")
    if not has_drive_access(settings):
        _stale_source_warning(report, "cube_acr", drive_auth_detail(settings))
    hey = None if briefs_only else (HeyReach(settings) if settings.heyreach_key else None)
    if briefs_only:
        if gmail and not settings.dry_run:
            briefing.send_due(settings, gmail, hs, memory, report)
        elif gmail and settings.dry_run:
            report.skipped.append("briefing skipped (dry-run)")
        else:
            report.errors.append("Gmail missing, cannot send briefs")
        _flush_memory_errors(memory, report)
        memory.finish_run(run_id, _finish_status(settings, report), report.as_dict())
        _flush_memory_errors(memory, report)
        return report

    engagements: list[Engagement] = list(calendar_creates)
    cube_backfill = False
    skip_cube_freshness = False
    if hasattr(memory, "latest_freshness"):
        try:
            cube_backfill = (
                memory.latest_freshness(
                    "cube_acr",
                    fallback_local=not getattr(memory, "use_supabase", False),
                )
                is None
            )
        except Exception as exc:
            logger.warning("cube_acr freshness read failed; skip backfill: %s", exc)
            report.warnings.append("cube_acr: freshness read failed, skip backfill")
            cube_backfill = False
            skip_cube_freshness = True
    if has_drive_access(settings):
        try:
            cube_events = cube_acr.scan(settings, backfill=cube_backfill, warnings=report.warnings)
            if cube_backfill:
                report.warnings.append(
                    f"cube_acr: one-time backfill ({getattr(settings, 'cube_lookback_days', 14)}d)"
                )
                for ev in cube_events:
                    ev.extra["skip_lookback"] = True
                    ev.extra["cube_backfill"] = True
            engagements += cube_events
            if hasattr(memory, "upsert_cube_call"):
                for ev in cube_events:
                    memory.upsert_cube_call(ev)
        except CubeAuthError as exc:
            _stale_source_warning(report, "cube_acr", str(exc))
        except Exception as exc:
            report.errors.append(f"cube_acr: {exc}")
    try:
        engagements += fireflies.scan(settings)
    except Exception as exc:
        report.errors.append(f"fireflies: {exc}")
    try:
        engagements += smartlead.scan(settings, errors=report.errors)
    except Exception as exc:
        report.errors.append(f"smartlead: {exc}")
    if hey:
        try:
            engagements += hey.recent_conversations()
        except Exception as exc:
            report.errors.append(f"heyreach inbox: {exc}")
    try:
        engagements += rvm.scan(settings)
    except Exception as exc:
        report.errors.append(f"rvm: {exc}")
    if gmail:
        try:
            known = {(ev.email or "").lower() for ev in engagements if ev.email}
            known |= {e.lower() for e in (getattr(hs, "scheduled_attendee_emails", None) or set())}
            known |= {e.lower() for e in (getattr(hs, "recent_attendee_emails", None) or set())}
            engagements += gmail_scan.scan_people(
                settings,
                gmail,
                hubspot=hs,
                memory=memory,
                known_emails=known,
                report=report,
            )
        except Exception as exc:
            report.errors.append(f"gmail_person: {exc}")

    held_this_cycle: list[Engagement] = []
    windowed: list[Engagement] = []
    for ev in engagements:
        if not _in_window(ev, settings) and ev.source not in {"heyreach"}:
            report.skipped.append(f"{ev.source}:{ev.external_id} outside window")
            continue
        _annotate_sales_context(ev, settings, hs)
        if policy.is_meeting_held(ev):
            held_this_cycle.append(ev)
        windowed.append(ev)

    upcoming = set(getattr(hs, "scheduled_attendee_emails", set()) or set())
    try:
        from crmbrain.evidence import build_timelines
        from crmbrain.reconcile import _attach_hubspot, _count_open_deals

        timelines = build_timelines(windowed)
        _attach_hubspot(hs, timelines)
        planned = _planned_unique_people(
            windowed,
            settings,
            hs,
            memory,
            timelines,
            upcoming,
            held_this_cycle,
            report.calendar_api_ok,
        )
        if budget.maybe_abort(planned, _count_open_deals(hs, timelines)):
            report.reconcile_aborted = True
            report.would_abort = True
            report.warnings.append(budget.abort_reason)
            if not settings.dry_run:
                report.skipped.append(budget.abort_reason)
            else:
                budget.aborted = False
    except Exception as exc:
        report.errors.append(f"budget abort: {exc}")

    skip_writes = bool(budget.aborted and not settings.dry_run)
    if skip_writes:
        skip_cube_freshness = True
    if not skip_writes:
        mail_events: list[Engagement] = []
        if gmail:
            try:
                mail_events = gmail_scan.scan(settings, gmail, hs, report)
                engagements.extend(mail_events)
                windowed.extend(mail_events)
                for ev in mail_events:
                    if policy.is_meeting_held(ev):
                        held_this_cycle.append(ev)
            except Exception as exc:
                report.errors.append(f"gmail: {exc}")
        intent.attach_person_intent(settings, windowed)
        for ev in windowed:
            if ev.source == "gmail" or ev.extra.get("create_new"):
                continue
            try:
                _handle_engagement(ev, settings, hs, memory, hey, report, budget=budget)
                _release_gmail_people_overflow(memory, ev)
            except Exception as exc:
                report.errors.append(f"{ev.source}:{ev.external_id}: {exc}")

        for ev in mail_events:
            try:
                apply_gmail_stage_update(
                    ev,
                    settings,
                    hs,
                    memory,
                    hey,
                    report,
                    held_events=held_this_cycle,
                    budget=budget,
                )
            except Exception as exc:
                report.errors.append(f"{ev.source}:{ev.external_id}: {exc}")

        try:
            reconcile.run(
                settings,
                hs,
                memory,
                report,
                [ev for ev in windowed if _in_window(ev, settings) or ev.source == "heyreach"],
                upcoming_emails=upcoming,
                dry_run=settings.dry_run,
                calendar_api_ok=report.calendar_api_ok,
                held_events=held_this_cycle,
                budget=budget,
                skip_abort=True,
            )
        except Exception as exc:
            report.errors.append(f"reconcile: {exc}")
        try:
            from crmbrain import renewals

            if not settings.dry_run:
                renewals.sweep(hs, report, settings, engagements=held_this_cycle)
        except Exception as exc:
            report.errors.append(f"renewals: {exc}")
    elif gmail:
        try:
            mail_events = gmail_scan.scan(settings, gmail, hs, report)
            engagements.extend(mail_events)
        except Exception as exc:
            report.errors.append(f"gmail: {exc}")

    if gmail and not briefs_only:
        try:
            if settings.dry_run:
                report.skipped.append("briefing skipped (dry-run)")
            else:
                briefing.send_due(settings, gmail, hs, memory, report)
        except Exception as exc:
            report.errors.append(f"briefing: {exc}")

    _record_staleness(
        memory,
        report,
        engagements,
        calendar_last=calendar_last,
        calendar_error=calendar_error,
        skip_sources={"cube_acr"} if skip_cube_freshness else None,
    )

    if hey and not settings.dry_run:
        try:
            _backfill_hubspot_invites(settings, hs, hey, memory, report)
        except Exception as exc:
            report.errors.append(f"heyreach backfill: {exc}")

    if not settings.dry_run:
        try:
            prune.run(hs, report)
        except Exception as exc:
            report.errors.append(f"prune: {exc}")
    else:
        report.skipped.append("prune skipped (dry-run)")

    if not settings.dry_run:
        _fire_ticker(settings, memory, report)
    _flush_memory_errors(memory, report)
    memory.finish_run(run_id, _finish_status(settings, report), report.as_dict())
    _flush_memory_errors(memory, report)
    return report


def _latest_source_at(engagements: list[Engagement], source: str) -> datetime | None:
    hits = [ev.occurred_at for ev in engagements if ev.source == source and ev.occurred_at]
    return max(hits) if hits else None


def _record_staleness(
    memory: Memory,
    report: CycleReport,
    engagements: list[Engagement],
    *,
    calendar_last: datetime | None = None,
    calendar_error: str = "",
    skip_sources: set[str] | None = None,
) -> None:
    observed = {
        "gmail": _latest_source_at(engagements, "gmail") or _latest_source_at(engagements, "gmail_person"),
        "fireflies": _latest_source_at(engagements, "fireflies"),
        "calendar": calendar_last,
        "cube_acr": _latest_source_at(engagements, "cube_acr"),
        "smartlead": _latest_source_at(engagements, "smartlead"),
    }
    errors = {}
    if calendar_error:
        errors["calendar"] = calendar_error
    for warn in report.warnings:
        if warn.lower().startswith("cube_acr"):
            errors["cube_acr"] = warn
    for err in report.errors:
        low = err.lower()
        for source in ("gmail", "fireflies", "cube_acr", "smartlead"):
            if low.startswith(source):
                errors[source] = err
    staleness.record_and_alarm(
        memory, report, observed, errors=errors, skip_sources=skip_sources
    )
