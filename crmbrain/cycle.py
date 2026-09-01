from __future__ import annotations

from datetime import timedelta

from crmbrain import briefing, enrichment, intelligence, slack_notify, ticker
from crmbrain.config import JOSH_EMAILS, STAGE, Settings, is_client_context, is_personal, lookback_start, now_utc
from crmbrain.gmail_client import Gmail
from crmbrain.heyreach import HeyReach
from crmbrain.hubspot import HubSpot
from crmbrain.memory import Memory
from crmbrain.models import CycleReport, Engagement
from crmbrain.leadmagic import find_profile, should_skip_email, usable_linkedin
from crmbrain.sources import allo, cube_acr, fireflies, gmail_scan, rvm, smartlead


def _in_window(ev: Engagement, settings: Settings) -> bool:
    start = lookback_start(settings.lookback_hours)
    if ev.source == "smartlead":
        # Positive replies stay working until they are in HubSpot. First cycle
        # still respects lookback so we do not dump the whole history.
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
    if memory.already_processed(ev.source, ev.external_id):
        report.skipped.append(f"{ev.source}:{ev.external_id} already processed")
        return
    if is_personal(name=ev.display_name(), phone=ev.phone, email=ev.email):
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

    ev = enrichment.enrich(settings, ev)
    contact = hs.upsert_contact(ev)
    report.contacts_upserted.append(f"{ev.display_name() or ev.email} ({ev.source})")
    facts = intelligence.extract(settings, ev)
    hs.patch_contact(contact["id"], intelligence.merge_contact_props(contact, facts))
    note = ev.summary or ev.transcript[:1500] or ev.raw_subject
    if note:
        hs.add_note(contact["id"], f"{ev.source} {ev.occurred_at or ''}\n\n{note}")
    for fact_type in ("personal_details", "family_notes", "relationship_hooks"):
        if facts.get(fact_type):
            memory.save_fact(
                {
                    "hs_contact_id": contact["id"],
                    "fact_type": fact_type,
                    "fact_text": facts[fact_type],
                    "source": ev.source,
                    "external_id": ev.external_id,
                }
            )

    hint = facts.get("stage_hint") or ev.stage_hint
    stage = intelligence.stage_id(hint) if hint else ""
    money_stages = {STAGE["signed"], STAGE["paid"], STAGE["proposal_sent"]}
    if ev.source != "gmail" and stage in money_stages:
        stage = ""
    if is_client_context(ev.display_name(), ev.company, ev.raw_subject):
        report.skipped.append(f"{ev.display_name()} client conversation, notes only")
        stage = ""
    elif ev.source == "smartlead" and not stage:
        stage = STAGE["nurture"]
    elif ev.source in {"cube_acr", "fireflies", "allo"} and not stage:
        stage = STAGE["discovery_completed"]
    elif ev.source in {"heyreach", "rvm"} and not stage:
        stage = STAGE["replied"]
    elif ev.source == "calendly" and not stage:
        stage = STAGE["discovery_scheduled"]
    if stage:
        deal = hs.upsert_deal(contact, ev, stage)
        report.deals_moved.append(f"{ev.display_name()} -> {stage} ({deal.get('id')})")
        if stage in {STAGE["discovery_scheduled"], STAGE["discovery_completed"], STAGE["paid"], STAGE["signed"]}:
            memory.stop_ticker(email=ev.email, hs_contact_id=contact["id"])

    reason = facts.get("ticker_reason") or ev.ticker_reason
    if ev.source == "smartlead" and not reason:
        reason = "never_booked"
    if reason:
        ticker.enroll(memory, ev, reason, hs_contact_id=contact["id"])
        report.ticker_enrolled.append(f"{ev.display_name()} {reason}")

    _queue_linkedin(settings, hey, ev, hs, memory, report, contact=contact)

    memory.mark_processed(ev.source, ev.external_id, {"contact_id": contact["id"]})
    report.processed.append(f"{ev.source}:{ev.external_id}")


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
        ev = enrichment.enrich(settings, ev)
        ev.linkedin_url = usable_linkedin(ev.linkedin_url)
    if not ev.linkedin_url and ev.email:
        ev.linkedin_url = find_profile(settings, ev.email)
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


def _fire_ticker(settings: Settings, memory: Memory, report: CycleReport) -> None:
    now = now_utc()
    due = memory.due_ticker(now.isoformat())
    for row in due:
        subject, body = ticker.draft_email(row.get("name") or "", row.get("company") or "", row.get("reason") or "")
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
    memory = Memory(settings)
    run_id = memory.start_run()
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
        memory.finish_run(run_id, "ok" if not report.errors else "partial", report.as_dict())
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

    _fire_ticker(settings, memory, report)
    memory.finish_run(run_id, "ok" if not report.errors else "partial", report.as_dict())
    return report
