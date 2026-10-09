"""Each cycle: archive Replied junk with no meeting evidence; soft-archive blanks.

When a junk deal is pruned, also archive the associated contact if it has no
meeting held/scheduled evidence and is not otherwise engaged (no other open
deals). System addresses (Fireflies Notetaker, calendar bots) never count as
meeting evidence. Notetaker/bot contacts are archived in every stage.
"""

from __future__ import annotations

from crmbrain.calendar_events import contact_on_calendar
from crmbrain.config import STAGE
from crmbrain.hubspot import HubSpot
from crmbrain.models import CycleReport
from crmbrain.deal_write import commit_deal_archive, commit_deal_move
from crmbrain.policy import (
    DEFAULT_PIPELINE,
    MEETING_STAGES,
    PRE_SALE_STAGES,
    clean_deal_name,
    contact_has_meeting_evidence,
    contact_has_protected_deal,
    deal_has_amount,
    deal_pipeline,
    is_blank_contact,
    promote_replied_stage,
)
from crmbrain.sources.gmail_scan import (
    NOTETAKER_DOMAINS,
    is_junk_crm_email,
    is_notetaker_contact,
)

DEAL_LIMIT = 40
CONTACT_LIMIT = 20
NOTETAKER_LIMIT = 40


def run(hs: HubSpot, report: CycleReport) -> None:
    prune_notetaker_contacts(hs, report)
    prune_replied_deals(hs, report)
    prune_blank_contacts(hs, report)


def _calendar_protect_emails(hs: HubSpot) -> set[str]:
    upcoming = {e.lower() for e in getattr(hs, "scheduled_attendee_emails", set()) or set()}
    recent = {e.lower() for e in getattr(hs, "recent_attendee_emails", set()) or set()}
    return upcoming | recent


def _calendar_promote_emails(hs: HubSpot) -> set[str]:
    return {e.lower() for e in getattr(hs, "scheduled_attendee_emails", set()) or set()}


def _meeting_evidence(hs: HubSpot, contact: dict, deals: list[dict] | None = None) -> bool:
    email = ((contact.get("properties") or {}).get("email") or "")
    if is_junk_crm_email(email) or is_notetaker_contact(contact):
        return False
    if contact_on_calendar(email, _calendar_protect_emails(hs)):
        return True
    if contact_has_meeting_evidence(contact, deals):
        return True
    cid = contact.get("id")
    if cid and hs.contact_has_meetings(str(cid)):
        return True
    return False


def has_live_meeting_evidence(
    hs: HubSpot,
    contact: dict,
    deals: list[dict] | None = None,
    *,
    exclude_deal_id: str = "",
) -> bool:
    """True when the contact may stay in HubSpot (held or scheduled meeting)."""
    if deals is None and contact.get("id"):
        deals = hs.open_deals_for_contact(contact["id"])
    if exclude_deal_id:
        deals = [d for d in (deals or []) if str(d.get("id") or "") != str(exclude_deal_id)]
    return _meeting_evidence(hs, contact, deals)


def _contact_label(contact: dict) -> str:
    props = contact.get("properties") or {}
    return (
        (props.get("email") or "").strip()
        or f"{props.get('firstname') or ''} {props.get('lastname') or ''}".strip()
        or str(contact.get("id") or "")
    )


def archive_unengaged_contact(
    hs: HubSpot,
    contact: dict,
    report: CycleReport,
    reason: str,
    *,
    force: bool = False,
) -> None:
    cid = contact.get("id")
    if not cid:
        return
    if not force:
        email = ((contact.get("properties") or {}).get("email") or "")
        if not is_junk_crm_email(email) and not is_notetaker_contact(contact):
            try:
                deals = hs.open_deals_for_contact(str(cid))
            except Exception:
                deals = []
            if contact_has_protected_deal(deals):
                return
    try:
        hs.archive_contact(str(cid))
    except Exception as exc:
        report.errors.append(f"prune contact {cid}: {exc}")
        return
    report.contacts_pruned.append(f"{_contact_label(contact)} ({reason})")


def _archive_deal_contact_if_junk(
    hs: HubSpot, contact: dict, deal_id: str, report: CycleReport
) -> None:
    """After a junk deal is archived, drop the contact if nothing else keeps it."""
    cid = contact.get("id")
    if not cid:
        return
    remaining = [
        d for d in hs.open_deals_for_contact(cid) if str(d.get("id") or "") != str(deal_id)
    ]
    if remaining:
        return
    if _meeting_evidence(hs, contact, remaining):
        return
    archive_unengaged_contact(hs, contact, report, "junk deal, no meeting")


def prune_replied_deals(hs: HubSpot, report: CycleReport, limit: int = DEAL_LIMIT) -> None:
    """Archive Appointment Scheduled deals that have no Calendly/Fireflies/GCal evidence.

    If the contact clearly held or booked a meeting, promote instead of deleting.
    Associated emails are not meeting evidence and never promote Replied.
    """
    scanned = 0
    for deal in hs.iter_deals(
        ["dealname", "dealstage", "pipeline", "crmbrain_locked"], stage=STAGE["replied"]
    ):
        if scanned >= limit:
            break
        scanned += 1
        deal_id = str(deal.get("id") or "")
        name = (deal.get("properties") or {}).get("dealname") or deal_id
        contacts = hs.contacts_for_deal(deal_id)
        promote = ""
        keep = False
        for contact in contacts:
            more = hs.open_deals_for_contact(contact["id"])
            if _meeting_evidence(hs, contact, more):
                keep = True
                email = ((contact.get("properties") or {}).get("email") or "")
                candidate = promote_replied_stage(
                    contact,
                    has_real_meetings=hs.contact_has_meetings(contact["id"]),
                    has_email_associations=False,
                    has_calendar_meeting=contact_on_calendar(email, _calendar_promote_emails(hs)),
                )
                if candidate:
                    promote = candidate
        if keep and promote:
            fallback = ""
            if contacts:
                props = (contacts[0].get("properties") or {})
                fallback = f"{props.get('firstname') or ''} {props.get('lastname') or ''}".strip() or (
                    props.get("email") or ""
                )
            cleaned = clean_deal_name(name, fallback=fallback)
            if commit_deal_move(
                hs,
                deal,
                promote,
                evidence="prune:meeting-evidence",
                dealname=cleaned if cleaned and cleaned != name else "",
                settings=getattr(hs, "settings", None),
                report=report,
            ):
                report.deals_moved.append(f"prune {cleaned} -> {promote}")
            continue
        if keep:
            continue
        if commit_deal_archive(
            hs, deal, settings=getattr(hs, "settings", None), report=report
        ):
            report.deals_pruned.append(name)
            for contact in contacts:
                _archive_deal_contact_if_junk(hs, contact, deal_id, report)


def prune_blank_contacts(hs: HubSpot, report: CycleReport, limit: int = CONTACT_LIMIT) -> None:
    """Soft-archive contacts with no identity and no meeting evidence."""
    archived = 0
    for contact in hs.iter_contacts(
        ["email", "firstname", "lastname", "phone", "company", "crm_source"]
    ):
        if archived >= limit:
            break
        if not is_blank_contact(contact):
            continue
        deals = hs.open_deals_for_contact(contact["id"])
        if _meeting_evidence(hs, contact, deals):
            continue
        if any((d.get("properties") or {}).get("dealstage") in MEETING_STAGES for d in deals):
            continue
        try:
            hs.archive_contact(contact["id"])
        except Exception as exc:
            report.errors.append(f"prune contact {contact.get('id')}: {exc}")
            continue
        archived += 1
        report.contacts_pruned.append(str(contact.get("id")))


def _deal_has_real_contact(hs: HubSpot, deal_id: str, bot_id: str) -> bool:
    for other in hs.contacts_for_deal(deal_id):
        if str(other.get("id") or "") == str(bot_id):
            continue
        if not is_notetaker_contact(other):
            return True
    return False


def may_archive_notetaker_deal(hs: HubSpot, deal: dict, bot_id: str) -> bool:
    """Only drop bot-only pre-sale stubs. Keep paid/signed/shared deals."""
    props = deal.get("properties") or {}
    stage = props.get("dealstage") or ""
    if deal_pipeline(deal) != DEFAULT_PIPELINE:
        return False
    if stage not in PRE_SALE_STAGES:
        return False
    if deal_has_amount(deal):
        return False
    deal_id = str(deal.get("id") or "")
    if deal_id and _deal_has_real_contact(hs, deal_id, bot_id):
        return False
    return True


def _archive_notetaker(hs: HubSpot, contact: dict, report: CycleReport) -> None:
    cid = contact.get("id")
    if not cid:
        return
    for deal in hs.open_deals_for_contact(cid):
        deal_id = str(deal.get("id") or "")
        if not deal_id:
            continue
        if may_archive_notetaker_deal(hs, deal, str(cid)):
            try:
                if commit_deal_archive(
                    hs, deal, settings=getattr(hs, "settings", None), report=report
                ):
                    report.deals_pruned.append(
                        (deal.get("properties") or {}).get("dealname") or deal_id
                    )
            except Exception as exc:
                report.errors.append(f"prune notetaker deal {deal_id}: {exc}")
            continue
        drop = getattr(hs, "disassociate_contact_from_deal", None)
        if callable(drop):
            try:
                drop(str(cid), deal_id)
            except Exception as exc:
                report.errors.append(f"detach notetaker {cid} from {deal_id}: {exc}")
    archive_unengaged_contact(hs, contact, report, "notetaker", force=True)


def prune_notetaker_contacts(hs: HubSpot, report: CycleReport, limit: int = NOTETAKER_LIMIT) -> None:
    """Always soft-archive Fireflies/Otter/etc. bots.

    Attached deals are archived only when they are default-pipeline pre-sale
    stubs with no amount and no real (non-notetaker) contact.
    """
    found: dict[str, dict] = {}
    search = getattr(hs, "search_contacts_by_email_token", None)
    if callable(search):
        for token in ("fred@fireflies.ai", *sorted(NOTETAKER_DOMAINS)):
            try:
                for row in search(token) or []:
                    cid = str(row.get("id") or "")
                    if cid:
                        found[cid] = row
            except Exception as exc:
                report.errors.append(f"notetaker search {token}: {exc}")
    scanned = 0
    for contact in hs.iter_contacts(
        ["email", "firstname", "lastname", "phone", "company", "crm_source"]
    ):
        scanned += 1
        if scanned > 400:
            break
        cid = str(contact.get("id") or "")
        if cid and is_notetaker_contact(contact):
            found[cid] = contact
    archived = 0
    for contact in found.values():
        if archived >= limit:
            break
        if not is_notetaker_contact(contact):
            continue
        before = len(report.contacts_pruned)
        _archive_notetaker(hs, contact, report)
        if len(report.contacts_pruned) > before:
            archived += 1
