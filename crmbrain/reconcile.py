"""Reconcile HubSpot to the per-person evidence timeline.

Each cycle: classify → create / restore / advance / regress / leave / review.
Never destructively touch Paid or Signed deals. Soft-archive only.
"""

from __future__ import annotations

import logging
from datetime import datetime

from crmbrain import evidence, intent, policy
from crmbrain.config import STAGE, Settings
from crmbrain.evidence import (
    KIND_BOOKED,
    KIND_CANCELED,
    KIND_HELD,
    KIND_NO_SHOW,
    KIND_PAYMENT,
    KIND_POC,
    KIND_PROPOSAL,
    KIND_SIGNED,
    PersonTimeline,
)
from crmbrain.hubspot import HubSpot
from crmbrain.memory import Memory
from crmbrain.models import CycleReport, Engagement, IntentDecision, ProposedWrite
from crmbrain.policy import CLOSED_WON_STAGES, STAGE_RANK

logger = logging.getLogger(__name__)

PROTECTED_STAGES = {STAGE["signed"], STAGE["paid"]}


def representative_engagement(timeline: PersonTimeline) -> Engagement:
    if timeline.engagements:
        ranked = sorted(
            timeline.engagements,
            key=lambda ev: (
                1 if ev.source in policy.MEETING_CRM_SOURCES else 0,
                ev.occurred_at or datetime.min,
            ),
            reverse=True,
        )
        return ranked[0]
    return Engagement(
        source="reconcile",
        external_id=timeline.key,
        email=timeline.email,
        phone=timeline.phone,
        first_name=timeline.first_name,
        last_name=timeline.last_name,
        name=timeline.name,
        company=timeline.company,
    )


def stage_from_timeline(
    timeline: PersonTimeline,
    decision: IntentDecision,
    *,
    has_upcoming: bool = False,
    canceled_no_reschedule: bool = False,
    past_grace: bool = False,
) -> str:
    """Latest evidence wins. Forward and back."""
    kinds = timeline.kinds()
    if KIND_PAYMENT in kinds:
        return STAGE["paid"]
    if KIND_SIGNED in kinds:
        return STAGE["signed"]
    if KIND_POC in kinds and decision.verdict == "yes":
        return STAGE["signed"]
    if KIND_PROPOSAL in kinds:
        return STAGE["proposal_sent"]
    if KIND_HELD in kinds:
        return STAGE["discovery_completed"]
    if canceled_no_reschedule and not has_upcoming:
        return STAGE["nurture"]
    if KIND_NO_SHOW in kinds and KIND_HELD not in kinds and not has_upcoming:
        return STAGE["no_show"]
    if KIND_BOOKED in kinds or has_upcoming:
        return STAGE["discovery_scheduled"]
    if past_grace and KIND_HELD not in kinds and not has_upcoming:
        if KIND_BOOKED in kinds or _current_stage(timeline) == STAGE["discovery_scheduled"]:
            return STAGE["no_show"]
    if decision.stage:
        if decision.stage in STAGE.values():
            return decision.stage
        return STAGE.get(decision.stage, "")
    return ""


def _current_stage(timeline: PersonTimeline) -> str:
    live = policy.live_open_deals(timeline.deals)
    if not live:
        return ""
    deal = max(live, key=policy.deal_richness)
    return (deal.get("properties") or {}).get("dealstage") or ""


def _open_deal(timeline: PersonTimeline) -> dict | None:
    live = policy.live_open_deals(timeline.deals)
    if not live:
        return None
    return max(live, key=policy.deal_richness)


def evidence_move(current: str, target: str, timeline: PersonTimeline) -> str | None:
    """Stage to write. Empty means leave alone. Never archive Paid/Signed."""
    if not target or current == target:
        return None
    if current in PROTECTED_STAGES and target not in {STAGE["paid"], STAGE["signed"], STAGE["proposal_sent"]}:
        return None
    if current == STAGE["paid"] and target != STAGE["paid"]:
        return None
    if target in {STAGE["nurture"], STAGE["no_show"]} and KIND_HELD in timeline.kinds():
        return None
    if current and STAGE_RANK.get(target, 0) == STAGE_RANK.get(current, 0):
        return None
    return target


def _propose(report: CycleReport, write: ProposedWrite) -> None:
    report.proposed_writes.append(write.as_line())


def apply_timeline(
    timeline: PersonTimeline,
    settings: Settings,
    hs: HubSpot,
    memory: Memory,
    report: CycleReport,
    *,
    has_upcoming: bool = False,
    canceled_no_reschedule: bool = False,
    past_grace: bool = False,
    dry_run: bool = False,
) -> IntentDecision:
    ev = representative_engagement(timeline)
    decision = intent.classify(settings, ev)
    if KIND_POC in timeline.kinds() and decision.verdict == "yes" and not decision.stage:
        decision.stage = "signed"
    target = stage_from_timeline(
        timeline,
        decision,
        has_upcoming=has_upcoming,
        canceled_no_reschedule=canceled_no_reschedule,
        past_grace=past_grace,
    )
    amount = timeline.amount() or decision.amount
    label = timeline.display_name()
    current = _current_stage(timeline)
    deal = _open_deal(timeline)

    if evidence.reply_only(timeline) and not has_upcoming:
        if deal and current == STAGE["discovery_scheduled"] and current not in PROTECTED_STAGES:
            _archive_reply_only(hs, deal, label, report, dry_run)
        elif not deal:
            report.skipped.append(f"{label} reply-only, no deal")
        return decision

    if intent.is_confident_non_sales(decision, settings.intent_min_confidence):
        if deal and current not in PROTECTED_STAGES and current not in {STAGE["proposal_sent"]}:
            report.skipped.append(f"{label} non-opportunity ({decision.intent})")
        if not deal:
            report.skipped.append(f"{label} {decision.intent}, skip HubSpot")
        return decision

    if not intent.is_confident_sales(decision, settings.intent_min_confidence):
        _queue_review(memory, report, timeline, decision)
        return decision

    if not target:
        if deal:
            return decision
        if evidence.has_meeting_evidence(timeline):
            target = STAGE["discovery_completed"] if KIND_HELD in timeline.kinds() else STAGE["discovery_scheduled"]
        else:
            _queue_review(memory, report, timeline, decision)
            return decision

    write_stage = evidence_move(current, target, timeline)
    if deal and not write_stage and not amount:
        return decision

    if dry_run:
        action = "move" if deal and write_stage else ("create" if not deal else "amount")
        _propose(
            report,
            ProposedWrite(
                action=action,
                label=label,
                stage=write_stage or current,
                amount=amount,
                contact_id=str((timeline.contact or {}).get("id") or ""),
                deal_id=str((deal or {}).get("id") or ""),
            ),
        )
        return decision

    contact = timeline.contact
    if not contact:
        contact = hs.upsert_contact(ev)
        timeline.contact = contact
        report.contacts_upserted.append(f"{label} (reconcile)")
    wrote = hs.upsert_deal(contact, ev, write_stage or target, amount=amount)
    if wrote.get("id"):
        if not deal:
            report.deals_restored.append(f"{label} -> {write_stage or target} ({wrote.get('id')})")
        elif write_stage:
            report.deals_moved.append(f"{label} -> {write_stage} ({wrote.get('id')})")
        if amount:
            report.amounts_set.append(f"{label} {amount}")
        if (write_stage or target) in {
            STAGE["discovery_scheduled"],
            STAGE["discovery_completed"],
            STAGE["signed"],
            STAGE["paid"],
        }:
            memory.stop_ticker(email=timeline.email, hs_contact_id=contact.get("id"))
    return decision


def restore_missing_deals(
    hs: HubSpot,
    settings: Settings,
    memory: Memory,
    report: CycleReport,
    timelines: dict[str, PersonTimeline],
    *,
    dry_run: bool = False,
) -> None:
    """If meeting evidence exists and there is no open deal, create or restore one."""
    for timeline in timelines.values():
        if _open_deal(timeline):
            continue
        if not evidence.has_meeting_evidence(timeline):
            contact = timeline.contact
            if not contact:
                continue
            if not policy.contact_has_meeting_evidence(contact, timeline.deals):
                continue
        ev = representative_engagement(timeline)
        decision = intent.classify(settings, ev)
        if not intent.is_confident_sales(decision, settings.intent_min_confidence):
            if decision.verdict != "no":
                _queue_review(memory, report, timeline, decision)
            continue
        target = stage_from_timeline(timeline, decision) or STAGE["discovery_completed"]
        label = timeline.display_name()
        if dry_run:
            _propose(report, ProposedWrite(action="restore", label=label, stage=target, amount=timeline.amount()))
            continue
        contact = timeline.contact or hs.upsert_contact(ev)
        timeline.contact = contact
        archived = _restore_archived_deal(hs, contact, ev, target)
        if archived:
            report.deals_restored.append(f"{label} restored {archived.get('id')}")
            continue
        deal = hs.upsert_deal(contact, ev, target, amount=timeline.amount())
        if deal.get("id"):
            report.deals_restored.append(f"{label} -> {target} ({deal.get('id')})")


def reeval_discovery_scheduled(
    hs: HubSpot,
    settings: Settings,
    memory: Memory,
    report: CycleReport,
    timelines: dict[str, PersonTimeline],
    upcoming_emails: set[str],
    *,
    dry_run: bool = False,
) -> None:
    """Stuck Discovery Scheduled: completed / no-show / nurture from latest evidence."""
    if not hasattr(hs, "iter_deals"):
        return
    try:
        deals = list(hs.iter_deals(["dealname", "dealstage", "amount"], stage=STAGE["discovery_scheduled"]))
    except Exception as exc:
        report.errors.append(f"reconcile scheduled: {exc}")
        return
    for deal in deals:
        deal_id = str(deal.get("id") or "")
        contacts = hs.contacts_for_deal(deal_id) if hasattr(hs, "contacts_for_deal") else []
        if not contacts:
            continue
        contact = contacts[0]
        props = contact.get("properties") or {}
        email = (props.get("email") or "").strip().lower()
        key = evidence.person_key(
            email,
            props.get("phone") or "",
            f"{props.get('firstname') or ''} {props.get('lastname') or ''}",
        )
        timeline = timelines.get(key) if key else None
        if timeline is None:
            timeline = PersonTimeline(
                key=key or f"deal:{deal_id}",
                email=email,
                phone=props.get("phone") or "",
                first_name=props.get("firstname") or "",
                last_name=props.get("lastname") or "",
                company=props.get("company") or "",
                contact=contact,
                deals=[deal],
            )
        else:
            timeline.contact = timeline.contact or contact
            if deal not in timeline.deals:
                timeline.deals.append(deal)
        has_upcoming = bool(email and email in upcoming_emails)
        canceled = KIND_CANCELED in timeline.kinds()
        past = not has_upcoming
        held = KIND_HELD in timeline.kinds() or (props.get("crm_source") or "").lower() in {
            "fireflies",
            "cube_acr",
            "allo",
        }
        if has_upcoming:
            continue
        if held:
            target = STAGE["discovery_completed"]
        elif canceled:
            target = STAGE["nurture"]
        elif past:
            target = STAGE["no_show"]
        else:
            continue
        label = timeline.display_name() or (deal.get("properties") or {}).get("dealname") or deal_id
        if dry_run:
            _propose(report, ProposedWrite(action="move", label=str(label), stage=target, deal_id=deal_id))
            continue
        ev = representative_engagement(timeline)
        ev.stage_hint = target
        wrote = hs.upsert_deal(contact, ev, target)
        if wrote.get("id"):
            report.deals_moved.append(f"{label} scheduled-reeval -> {target} ({wrote.get('id')})")
            if target == STAGE["no_show"]:
                from crmbrain import ticker

                ticker.enroll(memory, ev, "no_show", hs_contact_id=contact.get("id"))


def run(
    settings: Settings,
    hs: HubSpot,
    memory: Memory,
    report: CycleReport,
    engagements: list[Engagement],
    *,
    upcoming_emails: set[str] | None = None,
    dry_run: bool = False,
) -> dict[str, PersonTimeline]:
    upcoming_emails = {e.lower() for e in (upcoming_emails or set())}
    timelines = evidence.build_timelines(engagements)
    _attach_hubspot(hs, timelines)
    for timeline in timelines.values():
        email = timeline.email
        try:
            apply_timeline(
                timeline,
                settings,
                hs,
                memory,
                report,
                has_upcoming=bool(email and email in upcoming_emails),
                canceled_no_reschedule=KIND_CANCELED in timeline.kinds() and not (email and email in upcoming_emails),
                past_grace=not (email and email in upcoming_emails),
                dry_run=dry_run,
            )
        except Exception as exc:
            report.errors.append(f"reconcile {timeline.display_name()}: {exc}")
            logger.warning("reconcile failed %s: %s", timeline.display_name(), exc)
    try:
        restore_missing_deals(hs, settings, memory, report, timelines, dry_run=dry_run)
    except Exception as exc:
        report.errors.append(f"reconcile restore: {exc}")
    try:
        reeval_discovery_scheduled(
            hs, settings, memory, report, timelines, upcoming_emails, dry_run=dry_run
        )
    except Exception as exc:
        report.errors.append(f"reconcile scheduled: {exc}")
    return timelines


def _attach_hubspot(hs: HubSpot, timelines: dict[str, PersonTimeline]) -> None:
    if not hasattr(hs, "find_contact"):
        return
    for timeline in timelines.values():
        if timeline.contact:
            continue
        try:
            found = hs.find_contact(email=timeline.email, phone=timeline.phone, name=timeline.display_name())
        except Exception:
            found = None
        if not found:
            continue
        timeline.contact = found
        if hasattr(hs, "open_deals_for_contact") and found.get("id"):
            try:
                timeline.deals = hs.open_deals_for_contact(found["id"])
            except Exception:
                timeline.deals = []


def _archive_reply_only(hs: HubSpot, deal: dict, label: str, report: CycleReport, dry_run: bool) -> None:
    deal_id = str(deal.get("id") or "")
    if not deal_id:
        return
    if dry_run:
        _propose(report, ProposedWrite(action="archive", label=label, stage=STAGE["discovery_scheduled"], deal_id=deal_id))
        return
    hs.archive_deal(deal_id)
    report.deals_pruned.append(f"{label} reply-only")


def _queue_review(memory: Memory, report: CycleReport, timeline: PersonTimeline, decision: IntentDecision) -> None:
    line = f"{timeline.display_name()} {decision.verdict} {decision.confidence:.2f} {decision.reason}"
    report.review_queue.append(line)
    if hasattr(memory, "enqueue_review"):
        memory.enqueue_review(
            {
                "person_key": timeline.key,
                "email": timeline.email,
                "name": timeline.display_name(),
                "company": timeline.company,
                "intent": decision.intent,
                "confidence": decision.confidence,
                "reason": decision.reason,
                "evidence": {"kinds": sorted(timeline.kinds())},
            }
        )


def _restore_archived_deal(hs: HubSpot, contact: dict, ev: Engagement, stage: str) -> dict | None:
    find = getattr(hs, "find_archived_deal_for_contact", None)
    restore = getattr(hs, "restore_deal", None)
    if not callable(find) or not callable(restore):
        return None
    archived = find(contact.get("id") or "")
    if not archived:
        return None
    restored = restore(str(archived.get("id") or ""), stage)
    return restored
