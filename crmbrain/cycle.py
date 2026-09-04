from __future__ import annotations

import logging
from datetime import timedelta

from crmbrain import briefing, enrichment, intelligence, policy, prune, slack_notify, ticker
from crmbrain.config import JOSH_EMAILS, STAGE, Settings, is_personal, lookback_start, now_utc
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
    start = lookback_start(settings.lookback_hours)
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

    already = hs.find_contact(email=ev.email, phone=ev.phone, name=ev.display_name())
    if not policy.may_write_hubspot(ev, already is not None):
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
    """HubSpot is engaged people. Queue anyone not already sent to HeyReach."""
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
    )
    return [f"{name}: {'present' if ok else 'missing'}" for name, ok in checks]


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


def run(settings: Settings | None = None, briefs_only: bool = False) -> CycleReport:
    settings = settings or Settings.from_env()
    report = CycleReport()
    report.integrations.extend(integration_status(settings))
    memory = Memory(settings)
    run_id = memory.start_run()
    _flush_memory_errors(memory, report)
    if not settings.hubspot_token:
        report.errors.append("HUBSPOT_ACCESS_TOKEN missing")
        return report

    hs = HubSpot(settings)
    if not briefs_only:
        hs.ensure_properties()
    gmail = Gmail(settings) if settings.gmail_refresh_token else None
    hey = None if briefs_only else (HeyReach(settings) if settings.heyreach_key else None)
    if briefs_only:
        if gmail:
            briefing.send_due(settings, gmail, hs, memory, report)
        else:
            report.errors.append("Gmail missing, cannot send briefs")
        _flush_memory_errors(memory, report)
        memory.finish_run(run_id, "ok" if not report.errors else "partial", report.as_dict())
        _flush_memory_errors(memory, report)
        return report

    engagements: list[Engagement] = []
    try:
        engagements += cube_acr.scan(settings)
    except Exception as exc:
        report.errors.append(f"cube_acr: {exc}")
    try:
        engagements += fireflies.scan(settings)
    except Exception as exc:
        report.errors.append(f"fireflies: {exc}")
    try:
        engagements += smartlead.scan(settings)
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
        engagements += allo.scan(settings, gmail)
    except Exception as exc:
        report.errors.append(f"allo: {exc}")
    if gmail:
        try:
            engagements += gmail_scan.scan_people(settings, gmail)
        except Exception as exc:
            report.errors.append(f"gmail_person: {exc}")

    for ev in engagements:
        if not _in_window(ev, settings) and ev.source not in {"heyreach"}:
            report.skipped.append(f"{ev.source}:{ev.external_id} outside window")
            continue
        try:
            _handle_engagement(ev, settings, hs, memory, hey, report)
        except Exception as exc:
            report.errors.append(f"{ev.source}:{ev.external_id}: {exc}")

    if gmail:
        try:
            mail_events = gmail_scan.scan(settings, gmail, hs, report)
            for ev in mail_events:
                if memory.already_processed(ev.source, ev.external_id):
                    continue
                if ev.extra.get("create_new"):
                    ev.source = "calendly"
                    _handle_engagement(ev, settings, hs, memory, hey, report)
                    continue
                contact_id = ev.extra.get("hubspot_contact_id")
                if ev.stage_hint and contact_id:
                    contact = {"id": contact_id, "properties": {}}
                    deal = hs.upsert_deal(contact, ev, ev.stage_hint)
                    report.deals_moved.append(f"{ev.email} gmail -> {ev.stage_hint} ({deal.get('id')})")
                    if ev.stage_hint == STAGE["no_show"]:
                        ticker.enroll(memory, ev, "no_show", hs_contact_id=contact_id)
                        report.ticker_enrolled.append(f"{ev.email} no_show")
                    if ev.stage_hint in {STAGE["paid"], STAGE["signed"], STAGE["discovery_scheduled"]}:
                        memory.stop_ticker(email=ev.email, hs_contact_id=contact_id)
                if contact_id:
                    try:
                        found = hs.find_contact(email=ev.email) if ev.email else {"id": contact_id}
                    except Exception:
                        found = {"id": contact_id}
                    _queue_linkedin(settings, hey, ev, hs, memory, report, contact=found)
                memory.mark_processed(ev.source, ev.external_id, {"subject": ev.raw_subject})
                report.processed.append(f"gmail:{ev.raw_subject[:60]}")
            briefing.send_due(settings, gmail, hs, memory, report)
        except Exception as exc:
            report.errors.append(f"gmail: {exc}")

    if hey:
        try:
            _backfill_hubspot_invites(settings, hs, hey, memory, report)
        except Exception as exc:
            report.errors.append(f"heyreach backfill: {exc}")

    try:
        prune.run(hs, report)
    except Exception as exc:
        report.errors.append(f"prune: {exc}")

    _fire_ticker(settings, memory, report)
    _flush_memory_errors(memory, report)
    memory.finish_run(run_id, "ok" if not report.errors else "partial", report.as_dict())
    _flush_memory_errors(memory, report)
    return report
