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
from crmbrain.config import (
    JOSH_EMAILS,
    STAGE,
    Settings,
    compute_lookback_start,
    is_personal,
    now_utc,
    settings_lookback_start,
)
from crmbrain.gmail_client import Gmail
from crmbrain.heyreach import HeyReach
from crmbrain.hubspot import HubSpot
from crmbrain.memory import Memory
from crmbrain.models import CycleReport, Engagement
from crmbrain.leadmagic import should_skip_email, usable_linkedin
from crmbrain.sources import allo, cube_acr, fireflies, gmail_scan, rvm, smartlead
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


def _handle_engagement(
    ev: Engagement,
    settings: Settings,
    hs: HubSpot,
    memory: Memory,
    hey: HeyReach | None,
    report: CycleReport,
) -> None:
    if ev.email and is_junk_crm_email(ev.email):
        report.junk_blocked.append(f"{ev.source}:{ev.email} system address")
        memory.mark_processed(ev.source, ev.external_id, {"skip": "system_email"})
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
    already = hs.find_contact(email=ev.email, phone=ev.phone, name=ev.display_name())
    if ev.source in policy.HUBSPOT_CREATE_SOURCES and intent.is_confident_non_sales(
        decision, settings.intent_min_confidence
    ):
        report.skipped.append(f"{ev.source}:{ev.display_name() or ev.email} {decision.intent}")
        memory.mark_processed(ev.source, ev.external_id, {"skip": decision.intent})
        return
    if (
        ev.source in policy.HUBSPOT_CREATE_SOURCES
        and not intent.is_confident_sales(decision, settings.intent_min_confidence)
        and not already
    ):
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
        if already and meeting_evidence is False:
            prune.archive_unengaged_contact(hs, already, report, "no meeting")
        if memory.already_processed(ev.source, ev.external_id):
            report.skipped.append(f"{ev.source}:{ev.external_id} already processed")
            return
        reason = ev.ticker_reason or facts_reason_for_ticker(ev)
        if policy.should_enroll_ticker_without_hubspot(ev) and reason:
            ticker.enroll(memory, ev, reason)
            report.ticker_enrolled.append(f"{ev.display_name() or ev.email} {reason}")
        _queue_linkedin(settings, hey, ev, hs, memory, report, contact=None)
        report.skipped.append(
            f"{ev.source}:{ev.display_name() or ev.email or ev.phone} no meeting, skip HubSpot"
        )
        memory.mark_processed(ev.source, ev.external_id, {"skip": "no_meeting_hubspot"})
        report.processed.append(f"{ev.source}:{ev.external_id}")
        return

    if memory.already_processed(ev.source, ev.external_id):
        if ev.source in {"fireflies", "cube_acr"} and already:
            _apply_transcript_intelligence(
                ev, settings, hs, memory, report, already, add_timeline_note=False
            )
            report.skipped.append(f"{ev.source}:{ev.external_id} refreshed notes/amount")
        else:
            report.skipped.append(f"{ev.source}:{ev.external_id} already processed")
        return

    ev = enrichment.enrich(settings, ev)
    contact = hs.upsert_contact(ev)
    report.contacts_upserted.append(f"{ev.display_name() or ev.email} ({ev.source})")
    base = already or hs.find_contact(email=ev.email, phone=ev.phone, name=ev.display_name()) or contact
    base["id"] = contact["id"]
    facts = _apply_transcript_intelligence(
        ev, settings, hs, memory, report, base, add_timeline_note=True
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
) -> dict:
    """Always extract → merge_contact_props for meeting transcripts. Fill deal amount if empty."""
    facts = intelligence.extract(settings, ev)
    merged = intelligence.merge_contact_props(contact, facts)
    if merged:
        try:
            hs.patch_contact(contact["id"], merged)
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
                hs.add_note(contact["id"], f"{ev.source} {ev.occurred_at or ''}\n\n{note}")
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
    if not stage and policy.is_client_context_ev(ev):
        report.skipped.append(f"{ev.display_name()} client conversation, notes only")
    amount = facts.get("amount_hint") or facts.get("deal_amount") or ""
    if stage or amount:
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
            wrote_amount = hs.fill_deal_amount(deal, amount)
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
    if not hey or ev.source == "heyreach":
        return
    if is_personal(name=ev.display_name(), phone=ev.phone, email=ev.email):
        return
    if ev.email and (ev.email.lower() in JOSH_EMAILS or should_skip_email(ev.email)):
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
            hs.patch_contact(contact["id"], {"hs_linkedin_url": ev.linkedin_url})
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
        ("Cube folder", bool(settings.cube_folder)),
        ("Allo key", bool(settings.allo_key)),
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


def cycle_status(report: CycleReport) -> str:
    """ok unless data was skipped after exhausted retries or another hard error.

    Transient Smartlead 429/503 that later succeeded must not flip the cycle to
    partial. Those belong in logs, not report.errors; recovered notes are ignored.
    """
    for err in report.errors:
        if _recovered_rate_limit_note(err):
            continue
        return "partial"
    return "ok"


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
) -> None:
    """Apply a Gmail calendar/billing signal. Re-check held meetings before No Show."""
    already = memory.already_processed(ev.source, ev.external_id)
    contact = _mail_contact(hs, ev)
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
        _handle_engagement(ev, settings, hs, memory, hey, report)
        return
    if ev.stage_hint and contact_id:
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
            if not snap.calendar_api_ok:
                calendar_error = snap.calendar_api_error or "calendar api unavailable"
                report.errors.append(f"calendar: {calendar_error}")
        except Exception as exc:
            calendar_error = str(exc)
            logger.warning("calendar attendees unavailable: %s", exc)
            report.errors.append(f"calendar: {exc}")
    hey = None if briefs_only else (HeyReach(settings) if settings.heyreach_key else None)
    if briefs_only:
        if gmail:
            briefing.send_due(settings, gmail, hs, memory, report)
        else:
            report.errors.append("Gmail missing, cannot send briefs")
        _flush_memory_errors(memory, report)
        memory.finish_run(run_id, cycle_status(report), report.as_dict())
        _flush_memory_errors(memory, report)
        return report

    engagements: list[Engagement] = list(calendar_creates)
    try:
        cube_events = cube_acr.scan(settings)
        engagements += cube_events
        if hasattr(memory, "upsert_cube_call"):
            for ev in cube_events:
                memory.upsert_cube_call(ev)
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
    try:
        engagements += allo.scan(settings, gmail, memory=memory, errors=report.errors)
    except Exception as exc:
        report.errors.append(f"allo: {exc}")
    if gmail:
        try:
            engagements += gmail_scan.scan_people(settings, gmail)
        except Exception as exc:
            report.errors.append(f"gmail_person: {exc}")

    held_this_cycle: list[Engagement] = []
    for ev in engagements:
        if not _in_window(ev, settings) and ev.source not in {"heyreach"}:
            report.skipped.append(f"{ev.source}:{ev.external_id} outside window")
            continue
        if policy.is_meeting_held(ev):
            held_this_cycle.append(ev)
        try:
            _handle_engagement(ev, settings, hs, memory, hey, report)
        except Exception as exc:
            report.errors.append(f"{ev.source}:{ev.external_id}: {exc}")

    if gmail:
        try:
            mail_events = gmail_scan.scan(settings, gmail, hs, report)
            for ev in mail_events:
                apply_gmail_stage_update(
                    ev,
                    settings,
                    hs,
                    memory,
                    hey,
                    report,
                    held_events=held_this_cycle,
                )
            briefing.send_due(settings, gmail, hs, memory, report)
            engagements.extend(mail_events)
        except Exception as exc:
            report.errors.append(f"gmail: {exc}")

    try:
        reconcile.run(
            settings,
            hs,
            memory,
            report,
            [ev for ev in engagements if _in_window(ev, settings) or ev.source == "heyreach"],
            upcoming_emails=set(getattr(hs, "scheduled_attendee_emails", set()) or set()),
            dry_run=settings.dry_run,
        )
    except Exception as exc:
        report.errors.append(f"reconcile: {exc}")

    _record_staleness(memory, report, engagements, calendar_last=calendar_last, calendar_error=calendar_error)

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
    memory.finish_run(run_id, cycle_status(report), report.as_dict())
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
) -> None:
    observed = {
        "gmail": _latest_source_at(engagements, "gmail") or _latest_source_at(engagements, "gmail_person"),
        "fireflies": _latest_source_at(engagements, "fireflies"),
        "calendar": calendar_last,
        "allo": _latest_source_at(engagements, "allo"),
        "smartlead": _latest_source_at(engagements, "smartlead"),
    }
    errors = {}
    if calendar_error:
        errors["calendar"] = calendar_error
    for err in report.errors:
        low = err.lower()
        for source in ("gmail", "fireflies", "allo", "smartlead"):
            if low.startswith(source):
                errors[source] = err
    staleness.record_and_alarm(memory, report, observed, errors=errors)
