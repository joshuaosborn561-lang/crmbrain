from __future__ import annotations

from datetime import timedelta

from crmbrain import briefing, enrichment, intelligence, slack_notify, ticker
from crmbrain.config import STAGE, Settings, is_client_context, is_personal, lookback_start, now_utc
from crmbrain.gmail_client import Gmail
from crmbrain.heyreach import HeyReach
from crmbrain.hubspot import HubSpot
from crmbrain.memory import Memory
from crmbrain.models import CycleReport, Engagement
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

    if hey and (ev.linkedin_url or ev.email):
        try:
            if not ev.linkedin_url:
                ev = enrichment.enrich(settings, ev)
            if ev.linkedin_url:
                hey.add_lead(ev)
                report.linkedin_queued.append(ev.display_name() or ev.email)
        except Exception as exc:
            report.errors.append(f"heyreach {ev.display_name()}: {exc}")

    memory.mark_processed(ev.source, ev.external_id, {"contact_id": contact["id"]})
    report.processed.append(f"{ev.source}:{ev.external_id}")


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


def _brief_from_events(settings: Settings, gmail: Gmail, events: list[Engagement], report: CycleReport) -> None:
    """Upcoming meetings from Calendly-gated Gmail events in the next 48h."""
    horizon = now_utc() + timedelta(hours=48)
    for ev in events:
        if ev.stage_hint != STAGE["discovery_scheduled"]:
            continue
        if ev.occurred_at and ev.occurred_at > horizon:
            continue
        try:
            briefing.send(settings, gmail, ev)
            report.briefs_sent.append(ev.display_name() or ev.email)
        except Exception as exc:
            report.errors.append(f"brief {ev.email}: {exc}")


def run(settings: Settings | None = None) -> CycleReport:
    settings = settings or Settings.from_env()
    report = CycleReport()
    memory = Memory(settings)
    run_id = memory.start_run()
    if not settings.hubspot_token:
        report.errors.append("HUBSPOT_ACCESS_TOKEN missing")
        return report

    hs = HubSpot(settings)
    hs.ensure_properties()
    gmail = Gmail(settings) if settings.gmail_refresh_token else None
    hey = HeyReach(settings) if settings.heyreach_key else None

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
                memory.mark_processed(ev.source, ev.external_id, {"subject": ev.raw_subject})
                report.processed.append(f"gmail:{ev.raw_subject[:60]}")
            _brief_from_events(settings, gmail, mail_events, report)
        except Exception as exc:
            report.errors.append(f"gmail: {exc}")

    _fire_ticker(settings, memory, report)
    memory.finish_run(run_id, "ok" if not report.errors else "partial", report.as_dict())
    return report
