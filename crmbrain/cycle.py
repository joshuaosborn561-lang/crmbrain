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
from crmbrain.models import CycleReport, Engagement, ProposedWrite
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
    """Reserve one create or stage_move. Overflow → review_queue reason cap."""
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


def _annotate_sales_context(ev: Engagement, settings: Settings, hs: HubSpot, already: dict | None = None) -> dict | None:
    """Set intent flags / already_prospect from one cached classify + open pre-sale deals."""
    if ev.source not in {"cube_acr", "fireflies"}:
        return already
    decision = intent.classify(settings, ev)
    ev.extra["intent_yes"] = policy.cube_has_sales_intent(ev, decision, settings.intent_min_confidence)
    if already is None:
        try:
            already = hs.find_contact(email=ev.email, phone=ev.phone, name=ev.display_name())
        except Exception:
            already = None
    deals = _contact_deals(hs, already)
    ev.extra["already_prospect"] = policy.contact_is_prospect(already, deals)
    ev.extra["closed_won"] = policy.has_closed_won_deal(deals)
    return already


def _handle_budget_kind(ev: Engagement, already: dict | None, hs: HubSpot) -> str | None:
    """One create slot per new contact/deal; stage_move for an existing live deal."""
    deals = _contact_deals(hs, already)
    if policy.closed_won_notes_only(ev, deals):
        return None
    stage = policy.resolve_stage(ev)
    live = policy.live_open_deals(deals)
    creating_contact = already is None and policy.may_create_hubspot_contact(ev)
    creating_deal = bool(stage) and not live
    if ev.source in {"cube_acr", "fireflies"} and creating_deal and not policy.held_call_may_open_deal(ev):
        creating_deal = False
        creating_contact = False
    if creating_contact or creating_deal:
        return "create"
    if stage and live:
        current = (max(live, key=policy.deal_richness).get("properties") or {}).get("dealstage") or ""
        if policy.choose_deal_action(current, stage, ev):
            return "stage_move"
    return None


def _person_key_for(ev: Engagement) -> str:
    from crmbrain.evidence import person_key

    return person_key(ev.email, ev.phone, ev.display_name() or ev.name)


def _event_unprocessed(ev: Engagement, memory: Memory | None) -> bool:
    if memory is None or not hasattr(memory, "already_processed"):
        return True
    return not memory.already_processed(ev.source, ev.external_id)


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
    report.proposed_writes.append(
        ProposedWrite(
            action="create" if not already else "update",
            label=ev.display_name() or ev.email or ev.phone,
            stage=decision.stage or ev.stage_hint,
            reason=decision.reason,
            contact_id=str((already or {}).get("id") or ""),
        ).as_dict()
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
        name=ev.display_name() or ev.name,
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
    decision = intent.classify(settings, ev)
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
    if getattr(ev, "_person_intent", None) and intent.is_confident_no_intent(
        ev._person_intent, settings.intent_min_confidence
    ) and intent.person_has_client_commerce(ev):
        _record_person_intent_no(
            report, memory, ev, ev._person_intent, mark_processed=False
        )
    already = hs.find_contact(email=ev.email, phone=ev.phone, name=ev.display_name())
    already = _annotate_sales_context(ev, settings, hs, already)
    deals = _contact_deals(hs, already)
    notes_only = policy.closed_won_notes_only(ev, deals)
    if ev.source in {"cube_acr", "fireflies"}:
        salesish = policy.cube_has_sales_intent(ev, decision, settings.intent_min_confidence)
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
            ticker.enroll(memory, ev, reason)
            report.ticker_enrolled.append(f"{ev.display_name() or ev.email} {reason}")
        _queue_linkedin(settings, hey, ev, hs, memory, report, contact=None)
        report.skipped.append(skip_line)
        memory.mark_processed(ev.source, ev.external_id, {"skip": "no_meeting_hubspot"})
        report.processed.append(f"{ev.source}:{ev.external_id}")
        return

    if memory.already_processed(ev.source, ev.external_id):
        if settings.dry_run:
            report.skipped.append(f"{ev.source}:{ev.external_id} already processed")
            return
        if ev.source in {"fireflies", "cube_acr"} and already:
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
        kind = _handle_budget_kind(ev, already, hs)
        if not _reserve_budget(budget, kind, memory, report, ev):
            return
        _propose_engagement(report, ev, decision, already)
        return

    kind = _handle_budget_kind(ev, already, hs)
    if not _reserve_budget(budget, kind, memory, report, ev):
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
        ticker.enroll(memory, ev, reason, hs_contact_id=contact["id"])
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
    deals = _contact_deals(hs, contact)
    if policy.closed_won_notes_only(ev, deals):
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
    if stage or amount:
        live = policy.live_open_deals(deals)
        if ev.source in {"cube_acr", "fireflies"} and not live and not policy.held_call_may_open_deal(ev):
            return facts
        if reserved is None:
            extra_kind = None
            if stage and not live:
                extra_kind = "create"
            elif stage and live:
                current = (max(live, key=policy.deal_richness).get("properties") or {}).get("dealstage") or ""
                if policy.choose_deal_action(current, stage, ev):
                    extra_kind = "stage_move"
            if extra_kind and not _reserve_budget(budget, extra_kind, memory, report, ev):
                return facts
        try:
            deal = hs.upsert_deal(contact, ev, stage, amount=amount)
        except Exception as exc:
            report.errors.append(f"deal {ev.display_name() or ev.email}: {exc}")
            logger.warning("deal write failed %s: %s", ev.display_name() or ev.email, exc)
            return facts
        if deal.get("id") and stage:
            report.deals_moved.append(f"{ev.display_name()} -> {stage} ({deal.get('id')})")
            if stage in {STAGE["discovery_scheduled"], STAGE["discovery_completed"], STAGE["paid"], STAGE["signed"]}:
                memory.stop_ticker(email=ev.email, hs_contact_id=contact["id"])
        live_amount = (deal.get("properties") or {}).get("amount")
        wrote_amount = bool(deal.get("id") and amount and intelligence.amounts_equal(live_amount, amount))
        if deal.get("id") and amount and not wrote_amount:
            wrote_amount = hs.fill_deal_amount(deal, amount, ev=ev, contact=contact)
        if wrote_amount:
            report.amounts_set.append(f"{ev.display_name() or ev.email} {amount}")
            logger.info("amounts_set %s %s", ev.display_name() or ev.email, amount)
    return facts


def facts_reason_for_ticker(ev: Engagement) -> str:
    if ev.source == "smartlead":
        return ev.ticker_reason or "never_booked"
    if ev.source in {"heyreach", "rvm"}:
        return ev.ticker_reason or "never_booked"
    return ev.ticker_reason or ""


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


def _fire_ticker(settings: Settings, memory: Memory, report: CycleReport) -> None:
    now = now_utc()
    due = memory.due_ticker(now.isoformat())
    for row in due:
        subject, body = ticker.draft_email(
            row.get("name") or "",
            row.get("company") or "",
            row.get("reason") or "",
            extras=row,
        )
        text = (
            f"90-day ticker (approve before send)\n"
            f"To: {row.get('email') or row.get('phone')}\n"
            f"Why: {row.get('reason')}\n"
            f"Subject: {subject}\n\n{body}"
        )
        try:
            slack_notify.post(settings, text)
            report.ticker_drafts.append(row.get("email") or row.get("name") or row.get("id"))
        except Exception as exc:
            report.errors.append(f"slack ticker: {exc}")
        next_fire = (now + timedelta(days=90)).isoformat()
        memory.bump_ticker(str(row.get("id") or row.get("email")), next_fire, now.isoformat())


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
        name=ev.display_name() or ev.name,
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
    if getattr(ev, "_person_intent", None) and intent.is_confident_no_intent(
        ev._person_intent, settings.intent_min_confidence
    ) and intent.person_has_client_commerce(ev):
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
    if ev.stage_hint == STAGE["no_show"]:
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
        if already and not write_stage:
            report.skipped.append(f"{ev.source}:{ev.external_id} stale no_show")
            return
        if already and write_stage == STAGE["discovery_completed"] and contact_id:
            promote_kind = "create" if not policy.live_open_deals(_contact_deals(hs, contact)) else "stage_move"
            if settings.dry_run:
                if not _reserve_budget(budget, promote_kind, memory, report, ev):
                    return
                report.proposed_writes.append(
                    ProposedWrite(
                        action="move",
                        label=ev.email or ev.display_name(),
                        stage=write_stage,
                        contact_id=str(contact_id),
                        reason="held_beats_noshow",
                    ).as_dict()
                )
                return
            kind = "create" if not policy.live_open_deals(_contact_deals(hs, contact)) else "stage_move"
            if not _reserve_budget(budget, kind, memory, report, ev):
                return
            deal = hs.upsert_deal(contact or {"id": contact_id, "properties": {}}, ev, write_stage)
            if deal.get("id"):
                report.deals_moved.append(f"{ev.email} gmail -> {write_stage} ({deal.get('id')})")
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
    if ev.stage_hint and contact_id:
        gmail_kind = "create" if not policy.live_open_deals(_contact_deals(hs, contact)) else "stage_move"
        if settings.dry_run:
            if not _reserve_budget(budget, gmail_kind, memory, report, ev):
                return
            report.proposed_writes.append(
                ProposedWrite(
                    action="move",
                    label=ev.email or ev.display_name(),
                    stage=ev.stage_hint,
                    amount=str((ev.extra or {}).get("amount") or ""),
                    contact_id=str(contact_id),
                    reason="gmail",
                ).as_dict()
            )
            return
        if not _reserve_budget(budget, gmail_kind, memory, report, ev):
            return
        amount = str((ev.extra or {}).get("amount") or "")
        deal = hs.upsert_deal(contact or {"id": contact_id, "properties": {}}, ev, ev.stage_hint, amount=amount)
        report.deals_moved.append(f"{ev.email} gmail -> {ev.stage_hint} ({deal.get('id')})")
        if amount and deal.get("id"):
            report.amounts_set.append(f"{ev.email} {amount}")
        if ev.stage_hint == STAGE["no_show"]:
            ticker.enroll(memory, ev, "no_show", hs_contact_id=contact_id)
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
    settings = replace(settings, lookback_start_at=compute_lookback_start(settings, last_started))
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
            cube_events = cube_acr.scan(settings, backfill=cube_backfill)
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
