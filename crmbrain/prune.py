"""Each cycle: archive Replied junk with no meeting evidence; soft-archive blanks."""

from __future__ import annotations

from crmbrain.config import STAGE
from crmbrain.hubspot import HubSpot
from crmbrain.models import CycleReport
from crmbrain.policy import (
    MEETING_STAGES,
    clean_deal_name,
    contact_has_meeting_evidence,
    is_blank_contact,
    promote_replied_stage,
)

DEAL_LIMIT = 40
CONTACT_LIMIT = 20


def run(hs: HubSpot, report: CycleReport) -> None:
    prune_replied_deals(hs, report)
    prune_blank_contacts(hs, report)


def _meeting_evidence(hs: HubSpot, contact: dict, deals: list[dict] | None = None) -> bool:
    if contact_has_meeting_evidence(contact, deals):
        return True
    cid = contact.get("id")
    if cid and hs.contact_has_meetings(str(cid)):
        return True
    return False


def prune_replied_deals(hs: HubSpot, report: CycleReport, limit: int = DEAL_LIMIT) -> None:
    """Archive Appointment Scheduled deals that have no Calendly/Fireflies/GCal evidence.

    If the contact clearly held or booked a meeting, promote instead of deleting.
    Associated emails are not meeting evidence and never promote Replied.
    """
    scanned = 0
    for deal in hs.iter_deals(["dealname", "dealstage", "pipeline"], stage=STAGE["replied"]):
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
                candidate = promote_replied_stage(
                    contact,
                    has_real_meetings=hs.contact_has_meetings(contact["id"]),
                    has_email_associations=False,
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
            hs.move_deal(
                deal_id,
                promote,
                evidence="prune:meeting-evidence",
                dealname=cleaned if cleaned and cleaned != name else "",
            )
            report.deals_moved.append(f"prune {cleaned} -> {promote}")
            continue
        if keep:
            continue
        hs.archive_deal(deal_id)
        report.deals_pruned.append(name)


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
