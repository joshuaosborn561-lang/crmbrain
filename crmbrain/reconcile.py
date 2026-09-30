"""Reconcile HubSpot to the per-person evidence timeline.

Each cycle: classify → create / restore / advance / regress / leave / review.
Never destructively touch Paid or Signed deals. Soft-archive only.
"""

from __future__ import annotations

import logging
from datetime import datetime

from crmbrain import evidence, intent, policy, prune
from crmbrain.budget import WriteBudget
from crmbrain.config import STAGE, Settings, is_non_deal_person
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
from crmbrain.policy import CLOSED_WON_STAGES, NEVER_OPEN_DEAL_SOURCES, STAGE_RANK

logger = logging.getLogger(__name__)

PROTECTED_STAGES = {STAGE["signed"], STAGE["paid"]}
COLD_CREATE_SOURCES = NEVER_OPEN_DEAL_SOURCES | {"gmail", "gmail_person"}
MEETING_CRM_SOURCES = frozenset({"calendly", "fireflies", "cube_acr", "allo"})


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
    """Latest evidence wins. POC hints never become Signed."""
    kinds = timeline.kinds()
    if KIND_PAYMENT in kinds:
        return STAGE["paid"]
    if KIND_SIGNED in kinds:
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
        if decision.stage == STAGE["signed"] and KIND_SIGNED not in kinds and KIND_PAYMENT not in kinds:
            return STAGE["discovery_completed"] if KIND_HELD in kinds else ""
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


def evidence_move(current: str, target: str, timeline: PersonTimeline, ev: Engagement | None = None) -> str | None:
    """Stage to write. Empty means leave alone. Never archive Paid/Signed."""
    if not target or current == target:
        return None
    if current == STAGE["paid"]:
        return None
    if current == STAGE["signed"] and target == STAGE["proposal_sent"]:
        deal = _open_deal(timeline)
        if ev and policy.document_matches_deal(deal, ev):
            return target
        return None
    if current in PROTECTED_STAGES and target not in {STAGE["paid"], STAGE["signed"]}:
        return None
    if target in {STAGE["nurture"], STAGE["no_show"]} and KIND_HELD in timeline.kinds():
        return None
    if current and STAGE_RANK.get(target, 0) == STAGE_RANK.get(current, 0):
        return None
    return target


def _propose(report: CycleReport, write: ProposedWrite) -> None:
    report.proposed_writes.append(write.as_dict())


def _queue_review(
    memory: Memory,
    report: CycleReport,
    timeline: PersonTimeline,
    decision: IntentDecision | None = None,
    *,
    reason: str = "",
    dry_run: bool = False,
) -> None:
    why = reason or (decision.reason if decision else "review")
    label = timeline.display_name()
    conf = f"{decision.confidence:.2f}" if decision else ""
    verdict = decision.verdict if decision else ""
    line = " ".join(p for p in (label, verdict, conf, why) if p)
    if line in report.review_queue:
        return
    report.review_queue.append(line)
    if dry_run:
        return
    if hasattr(memory, "enqueue_review"):
        memory.enqueue_review(
            {
                "person_key": timeline.key,
                "email": timeline.email,
                "name": label,
                "company": timeline.company,
                "intent": decision.intent if decision else "",
                "confidence": decision.confidence if decision else 0.0,
                "reason": why,
                "evidence": {"kinds": sorted(timeline.kinds())},
            }
        )


def _commit(
    hs: HubSpot,
    memory: Memory,
    report: CycleReport,
    budget: WriteBudget,
    write: ProposedWrite,
    *,
    current: str,
    contact: dict | None,
    ev: Engagement,
    timeline: PersonTimeline,
    deal: dict | None,
    dry_run: bool,
) -> bool:
    if is_non_deal_person(
        name=timeline.display_name(),
        email=timeline.email,
        company=timeline.company,
        phone=timeline.phone,
    ) or is_non_deal_person(
        name=ev.display_name() or ev.name,
        email=ev.email,
        company=ev.company,
        phone=ev.phone,
    ):
        report.skipped.append(f"reconcile:{timeline.display_name() or ev.email} excluded")
        return False
    if budget.aborted:
        _queue_review(memory, report, timeline, reason="cap", dry_run=dry_run)
        return False
    kind = budget.classify(write.action, current, write.stage)
    if not budget.allow(kind):
        write.reason = "cap"
        _queue_review(memory, report, timeline, reason="cap", dry_run=dry_run)
        return False
    _propose(report, write)
    if dry_run:
        return True
    if write.action == "archive" and deal:
        hs.archive_deal(str(deal.get("id") or ""))
        report.deals_pruned.append(f"{write.label} {write.reason or 'archive'}")
        return True
    if not contact:
        if ev.source in COLD_CREATE_SOURCES:
            _queue_review(memory, report, timeline, reason="cold_source", dry_run=dry_run)
            return False
        contact = hs.upsert_contact(ev)
        timeline.contact = contact
        report.contacts_upserted.append(f"{write.label} (reconcile)")
    wrote = hs.upsert_deal(contact, ev, write.stage or current, amount=write.amount)
    if wrote.get("id"):
        if write.action in {"create", "restore"}:
            report.deals_restored.append(f"{write.label} -> {write.stage} ({wrote.get('id')})")
        elif write.stage:
            report.deals_moved.append(f"{write.label} -> {write.stage} ({wrote.get('id')})")
        if write.amount:
            report.amounts_set.append(f"{write.label} {write.amount}")
        if write.stage in {
            STAGE["discovery_scheduled"],
            STAGE["discovery_completed"],
            STAGE["signed"],
            STAGE["paid"],
        }:
            memory.stop_ticker(email=timeline.email, hs_contact_id=contact.get("id"))
    return True


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
    budget: WriteBudget | None = None,
    held_events: list[Engagement] | None = None,
) -> IntentDecision:
    del held_events
    budget = budget or WriteBudget.from_settings(settings)
    ev = representative_engagement(timeline)
    decision = intent.classify(settings, ev)
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
    kinds = timeline.kinds()

    if (KIND_POC in kinds or policy.has_poc_evidence(ev)) and KIND_SIGNED not in kinds and KIND_PAYMENT not in kinds:
        _queue_review(memory, report, timeline, decision, reason="poc_hint", dry_run=dry_run)
        if target == STAGE["signed"]:
            target = STAGE["discovery_completed"] if KIND_HELD in kinds else current or ""

    if evidence.reply_only(timeline) and not has_upcoming:
        if deal and current == STAGE["discovery_scheduled"] and current not in PROTECTED_STAGES:
            if _may_archive_reply_only(hs, timeline.contact, deal):
                _commit(
                    hs,
                    memory,
                    report,
                    budget,
                    ProposedWrite(
                        action="archive",
                        label=label,
                        stage=current,
                        deal_id=str(deal.get("id") or ""),
                        contact_id=str((timeline.contact or {}).get("id") or ""),
                        reason="reply-only",
                    ),
                    current=current,
                    contact=timeline.contact,
                    ev=ev,
                    timeline=timeline,
                    deal=deal,
                    dry_run=dry_run,
                )
            else:
                report.skipped.append(f"{label} reply-only, keep (meeting evidence)")
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
        _queue_review(memory, report, timeline, decision, dry_run=dry_run)
        return decision

    if policy.has_closed_won_deal(timeline.deals) and not deal:
        _queue_review(memory, report, timeline, decision, reason="closed_won_exists", dry_run=dry_run)
        return decision

    if not target:
        if deal:
            return decision
        if evidence.has_meeting_evidence(timeline):
            target = STAGE["discovery_completed"] if KIND_HELD in timeline.kinds() else STAGE["discovery_scheduled"]
        else:
            _queue_review(memory, report, timeline, decision, dry_run=dry_run)
            return decision

    if current == STAGE["signed"] and target == STAGE["proposal_sent"] and not policy.document_matches_deal(deal, ev):
        _queue_review(memory, report, timeline, decision, reason="signed_document_mismatch", dry_run=dry_run)
        return decision

    write_stage = evidence_move(current, target, timeline, ev)
    if deal and not write_stage and not amount:
        return decision

    if not deal and ev.source in COLD_CREATE_SOURCES:
        _queue_review(memory, report, timeline, decision, reason="cold_source", dry_run=dry_run)
        return decision

    action = "move" if deal and write_stage else ("create" if not deal else "amount")
    _commit(
        hs,
        memory,
        report,
        budget,
        ProposedWrite(
            action=action,
            label=label,
            stage=write_stage or current,
            amount=amount,
            contact_id=str((timeline.contact or {}).get("id") or ""),
            deal_id=str((deal or {}).get("id") or ""),
            reason=decision.reason,
        ),
        current=current,
        contact=timeline.contact,
        ev=ev,
        timeline=timeline,
        deal=deal,
        dry_run=dry_run,
    )
    return decision


def restore_missing_deals(
    hs: HubSpot,
    settings: Settings,
    memory: Memory,
    report: CycleReport,
    timelines: dict[str, PersonTimeline],
    *,
    dry_run: bool = False,
    budget: WriteBudget | None = None,
) -> None:
    """If meeting evidence exists and there is no open deal, create or restore one."""
    budget = budget or WriteBudget.from_settings(settings)
    for timeline in timelines.values():
        if is_non_deal_person(
            name=timeline.display_name(),
            email=timeline.email,
            company=timeline.company,
            phone=timeline.phone,
        ):
            continue
        if _open_deal(timeline):
            continue
        if policy.has_closed_won_deal(timeline.deals):
            _queue_review(memory, report, timeline, reason="closed_won_exists", dry_run=dry_run)
            continue
        if not evidence.has_meeting_evidence(timeline):
            contact = timeline.contact
            if not contact:
                continue
            if not policy.contact_has_meeting_evidence(contact, timeline.deals):
                continue
        ev = representative_engagement(timeline)
        if ev.source in COLD_CREATE_SOURCES and not evidence.has_meeting_evidence(timeline):
            continue
        decision = intent.classify(settings, ev)
        if not intent.is_confident_sales(decision, settings.intent_min_confidence):
            if decision.verdict != "no":
                _queue_review(memory, report, timeline, decision, dry_run=dry_run)
            continue
        target = stage_from_timeline(timeline, decision) or STAGE["discovery_completed"]
        if target == STAGE["signed"] and KIND_SIGNED not in timeline.kinds() and KIND_PAYMENT not in timeline.kinds():
            target = STAGE["discovery_completed"] if KIND_HELD in timeline.kinds() else STAGE["discovery_scheduled"]
        label = timeline.display_name()
        if dry_run:
            kind = budget.classify("restore", "", target)
            if budget.aborted or not budget.allow(kind):
                _queue_review(memory, report, timeline, decision, reason="cap", dry_run=True)
                continue
            _propose(report, ProposedWrite(action="restore", label=label, stage=target, amount=timeline.amount()))
            continue
        kind = budget.classify("restore", "", target)
        if budget.aborted or not budget.allow(kind):
            _queue_review(memory, report, timeline, decision, reason="cap", dry_run=dry_run)
            continue
        contact = timeline.contact
        if not contact:
            if ev.source in COLD_CREATE_SOURCES:
                _queue_review(memory, report, timeline, decision, reason="cold_source", dry_run=dry_run)
                continue
            contact = hs.upsert_contact(ev)
        timeline.contact = contact
        archived = _restore_archived_deal(hs, contact, ev, target)
        if archived:
            report.deals_restored.append(f"{label} restored {archived.get('id')}")
            _propose(
                report,
                ProposedWrite(action="restore", label=label, stage=target, deal_id=str(archived.get("id") or "")),
            )
            continue
        deal = hs.upsert_deal(contact, ev, target, amount=timeline.amount())
        if deal.get("id"):
            report.deals_restored.append(f"{label} -> {target} ({deal.get('id')})")
            _propose(
                report,
                ProposedWrite(action="restore", label=label, stage=target, deal_id=str(deal.get("id") or "")),
            )


def _scheduled_at(timeline: PersonTimeline, deal: dict | None) -> datetime | None:
    for ev in timeline.engagements:
        stamp = policy.scheduled_at_from_engagement(ev)
        if stamp:
            return stamp
    for event in timeline.events:
        stamp = policy.parse_iso_datetime((event.extra or {}).get("meeting_at") or (event.extra or {}).get("scheduled_at"))
        if stamp:
            return stamp
    props = (deal or {}).get("properties") or {}
    return policy.parse_iso_datetime(props.get("meeting_at") or props.get("scheduled_at"))


def _calendar_blocks_back_move(hs: HubSpot, email: str, contact: dict | None) -> bool:
    """Upcoming/recent calendar or a future HubSpot meeting blocks No Show / Nurture."""
    if _attendee_hit(hs, email):
        return True
    if contact and contact.get("id") and hasattr(hs, "contact_has_future_meetings"):
        try:
            return bool(hs.contact_has_future_meetings(str(contact["id"])))
        except Exception:
            return True
    return False


def _explicit_cancel(timeline: PersonTimeline) -> bool:
    if KIND_CANCELED in timeline.kinds():
        return True
    for ev in timeline.engagements:
        extra = ev.extra or {}
        if extra.get("canceled") or extra.get("cancelled"):
            return True
        blob = f"{ev.raw_subject} {ev.summary}".lower()
        if any(
            w in blob
            for w in (
                "canceled:",
                "cancelled:",
                "invitee canceled",
                "invitee cancelled",
                "canceled your event",
                "cancelled your event",
            )
        ):
            return True
    return False


def _attendee_hit(hs: HubSpot, email: str) -> bool:
    low = (email or "").strip().lower()
    if not low:
        return False
    upcoming = {e.lower() for e in (getattr(hs, "scheduled_attendee_emails", None) or set())}
    recent = {e.lower() for e in (getattr(hs, "recent_attendee_emails", None) or set())}
    return low in upcoming or low in recent


def _may_archive_reply_only(hs: HubSpot, contact: dict | None, deal: dict) -> bool:
    if not contact or not deal:
        return False
    source = ((contact.get("properties") or {}).get("crm_source") or "").lower()
    if source in MEETING_CRM_SOURCES:
        return False
    deal_id = str(deal.get("id") or "")
    if prune.has_live_meeting_evidence(hs, contact, exclude_deal_id=deal_id):
        return False
    cid = contact.get("id")
    if cid and hasattr(hs, "contact_has_meetings") and hs.contact_has_meetings(str(cid)):
        return False
    email = ((contact.get("properties") or {}).get("email") or "").strip().lower()
    if _attendee_hit(hs, email):
        return False
    return True


def _reeval_decision(
    hs: HubSpot,
    timeline: PersonTimeline,
    deal: dict,
    contact: dict,
    upcoming_emails: set[str],
    held_events: list[Engagement],
) -> tuple[str, str]:
    """Return (target_stage, reason). Empty target means no write.

    reason ``unknown_scheduled_time`` means review, not a HubSpot write.
    """
    email = ((contact.get("properties") or {}).get("email") or timeline.email or "").strip().lower()
    ev = representative_engagement(timeline)
    scheduled_at = _scheduled_at(timeline, deal)
    matched_held = policy.matching_held_event(ev, contact, held_events, scheduled_at)
    has_upcoming = bool(email and email in upcoming_emails)
    calendar_or_future = _calendar_blocks_back_move(hs, email, contact)
    canceled = _explicit_cancel(timeline)
    if matched_held:
        return STAGE["discovery_completed"], "held_this_cycle"
    if canceled and not has_upcoming and not calendar_or_future:
        return STAGE["nurture"], "canceled_no_reschedule"
    if not scheduled_at:
        return "", "unknown_scheduled_time"
    if (
        policy.scheduled_past_grace(scheduled_at)
        and not matched_held
        and not has_upcoming
        and not calendar_or_future
    ):
        return STAGE["no_show"], "past_grace_no_show"
    return "", ""


def reeval_discovery_scheduled(
    hs: HubSpot,
    settings: Settings,
    memory: Memory,
    report: CycleReport,
    timelines: dict[str, PersonTimeline],
    upcoming_emails: set[str],
    *,
    dry_run: bool = False,
    calendar_api_ok: bool = True,
    held_events: list[Engagement] | None = None,
    budget: WriteBudget | None = None,
) -> None:
    """Stuck Discovery Scheduled: completed / no-show / nurture from latest evidence."""
    if not calendar_api_ok:
        report.skipped.append("scheduled-reeval skipped (calendar api unavailable)")
        return
    budget = budget or WriteBudget.from_settings(settings)
    held_events = held_events or []
    if not hasattr(hs, "iter_deals"):
        return
    try:
        deals = list(hs.iter_deals(["dealname", "dealstage", "amount", "meeting_at"], stage=STAGE["discovery_scheduled"]))
    except Exception as exc:
        report.errors.append(f"reconcile scheduled: {exc}")
        return
    upcoming = {e.lower() for e in upcoming_emails}
    for deal in deals:
        deal_id = str(deal.get("id") or "")
        contacts = hs.contacts_for_deal(deal_id) if hasattr(hs, "contacts_for_deal") else []
        if not contacts:
            continue
        contact = contacts[0]
        props = contact.get("properties") or {}
        if is_non_deal_person(
            name=f"{props.get('firstname') or ''} {props.get('lastname') or ''}".strip(),
            email=props.get("email") or "",
            company=props.get("company") or "",
            phone=props.get("phone") or "",
        ):
            continue
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
        target, reason = _reeval_decision(
            hs, timeline, deal, contact, upcoming, held_events
        )
        label = timeline.display_name() or (deal.get("properties") or {}).get("dealname") or deal_id
        if reason == "unknown_scheduled_time":
            _queue_review(memory, report, timeline, reason="unknown_scheduled_time", dry_run=dry_run)
            continue
        if not target or target == STAGE["discovery_scheduled"]:
            continue
        kind = budget.classify("move", STAGE["discovery_scheduled"], target)
        if budget.aborted or not budget.allow(kind):
            _queue_review(memory, report, timeline, reason="cap", dry_run=dry_run)
            continue
        _propose(
            report,
            ProposedWrite(action="move", label=str(label), stage=target, deal_id=deal_id, reason=reason),
        )
        if dry_run:
            continue
        ev = representative_engagement(timeline)
        ev.stage_hint = target
        wrote = hs.upsert_deal(contact, ev, target)
        if wrote.get("id"):
            report.deals_moved.append(f"{label} scheduled-reeval -> {target} ({wrote.get('id')})")
            if target == STAGE["no_show"]:
                from crmbrain import ticker

                ticker.enroll(memory, ev, "no_show", hs_contact_id=contact.get("id"))


def _count_open_deals(hs: HubSpot, timelines: dict[str, PersonTimeline]) -> int:
    if hasattr(hs, "count_open_deals"):
        try:
            n = hs.count_open_deals()
            if n:
                return n
        except Exception:
            pass
    seen: set[str] = set()
    n = 0
    if hasattr(hs, "iter_deals"):
        try:
            for deal in hs.iter_deals(["dealstage"]):
                stage = (deal.get("properties") or {}).get("dealstage") or ""
                if stage and stage != STAGE["closed_lost"]:
                    n += 1
                    seen.add(str(deal.get("id") or ""))
        except Exception:
            pass
    if n:
        return n
    for timeline in timelines.values():
        for deal in timeline.deals:
            did = str(deal.get("id") or "")
            if did in seen:
                continue
            stage = (deal.get("properties") or {}).get("dealstage") or ""
            if stage and stage != STAGE["closed_lost"]:
                n += 1
                seen.add(did)
    if hasattr(hs, "deals"):
        for deal in getattr(hs, "deals") or []:
            did = str(deal.get("id") or "")
            if did in seen:
                continue
            stage = (deal.get("properties") or {}).get("dealstage") or ""
            if stage and stage != STAGE["closed_lost"]:
                n += 1
    return n


def _planned_change_count(
    hs: HubSpot,
    settings: Settings,
    timelines: dict[str, PersonTimeline],
    upcoming_emails: set[str],
    held_events: list[Engagement],
    calendar_api_ok: bool,
) -> int:
    """How many open deals this cycle would actually move/archive/create."""
    changed: set[str] = set()
    creates = 0
    for timeline in timelines.values():
        ev = representative_engagement(timeline)
        decision = intent.classify(settings, ev)
        current = _current_stage(timeline)
        deal = _open_deal(timeline)
        email = timeline.email
        canceled = KIND_CANCELED in timeline.kinds() and not (email and email in upcoming_emails)
        if canceled and _calendar_blocks_back_move(hs, email, timeline.contact):
            canceled = False
        target = stage_from_timeline(
            timeline,
            decision,
            has_upcoming=bool(email and email in upcoming_emails),
            canceled_no_reschedule=canceled,
            past_grace=not (email and email in upcoming_emails),
        )
        deal_id = str((deal or {}).get("id") or "")
        if evidence.reply_only(timeline) and deal and current == STAGE["discovery_scheduled"]:
            if deal_id:
                changed.add(deal_id)
            continue
        write = evidence_move(current, target, timeline, ev) if target else None
        if write and deal_id:
            changed.add(deal_id)
        elif not deal and target and intent.is_confident_sales(decision, settings.intent_min_confidence):
            creates += 1
    if calendar_api_ok and hasattr(hs, "iter_deals"):
        try:
            for deal in hs.iter_deals(["dealname", "dealstage", "meeting_at"], stage=STAGE["discovery_scheduled"]):
                deal_id = str(deal.get("id") or "")
                if not deal_id or deal_id in changed:
                    continue
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
                        contact=contact,
                        deals=[deal],
                    )
                target, reason = _reeval_decision(
                    hs, timeline, deal, contact, upcoming_emails, held_events
                )
                if target and reason != "unknown_scheduled_time":
                    changed.add(deal_id)
        except Exception:
            pass
    return creates + len(changed)


def run(
    settings: Settings,
    hs: HubSpot,
    memory: Memory,
    report: CycleReport,
    engagements: list[Engagement],
    *,
    upcoming_emails: set[str] | None = None,
    dry_run: bool = False,
    calendar_api_ok: bool = True,
    held_events: list[Engagement] | None = None,
    budget: WriteBudget | None = None,
) -> dict[str, PersonTimeline]:
    upcoming_emails = {e.lower() for e in (upcoming_emails or set())}
    held_events = held_events or []
    budget = budget or WriteBudget.from_settings(settings)
    timelines = evidence.build_timelines(engagements)
    _attach_hubspot(hs, timelines)
    would = _planned_change_count(hs, settings, timelines, upcoming_emails, held_events, calendar_api_ok)
    open_n = _count_open_deals(hs, timelines)
    if budget.maybe_abort(would, open_n):
        report.reconcile_aborted = True
        report.would_abort = True
        report.warnings.append(budget.abort_reason)
        if not dry_run:
            report.skipped.append(budget.abort_reason)
            return timelines
        budget.aborted = False
    for timeline in timelines.values():
        if is_non_deal_person(
            name=timeline.display_name(),
            email=timeline.email,
            company=timeline.company,
            phone=timeline.phone,
        ):
            report.skipped.append(f"reconcile:{timeline.display_name() or timeline.email} excluded")
            continue
        email = timeline.email
        canceled = KIND_CANCELED in timeline.kinds() and not (email and email in upcoming_emails)
        if canceled and _calendar_blocks_back_move(hs, email, timeline.contact):
            canceled = False
        try:
            apply_timeline(
                timeline,
                settings,
                hs,
                memory,
                report,
                has_upcoming=bool(email and email in upcoming_emails),
                canceled_no_reschedule=canceled,
                past_grace=not (email and email in upcoming_emails),
                dry_run=dry_run,
                budget=budget,
                held_events=held_events,
            )
        except Exception as exc:
            report.errors.append(f"reconcile {timeline.display_name()}: {exc}")
            logger.warning("reconcile failed %s: %s", timeline.display_name(), exc)
    try:
        restore_missing_deals(hs, settings, memory, report, timelines, dry_run=dry_run, budget=budget)
    except Exception as exc:
        report.errors.append(f"reconcile restore: {exc}")
    try:
        reeval_discovery_scheduled(
            hs,
            settings,
            memory,
            report,
            timelines,
            upcoming_emails,
            dry_run=dry_run,
            calendar_api_ok=calendar_api_ok,
            held_events=held_events,
            budget=budget,
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
