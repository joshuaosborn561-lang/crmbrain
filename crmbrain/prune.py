"""Each cycle: archive Replied junk with no meeting evidence; soft-archive blanks."""

from __future__ import annotations

from crmbrain.config import STAGE
from crmbrain.hubspot import HubSpot
from crmbrain.models import CycleReport
from crmbrain.policy import MEETING_STAGES, contact_has_meeting_evidence, is_blank_contact

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
    """
    scanned = 0
    for deal in hs.iter_deals(["dealname", "dealstage", "pipeline"], stage=STAGE["replied"]):
        if scanned >= limit:
            break
        scanned += 1
        deal_id = str(deal.get("id") or "")
        name = (deal.get("properties") or {}).get("dealname") or deal_id
        contacts = hs.contacts_for_deal(deal_id)
        deals_for_contact: list[dict] = [deal]
        promote = ""
        keep = False
        for contact in contacts:
            more = hs.open_deals_for_contact(contact["id"])
            deals_for_contact.extend(more)
            if _meeting_evidence(hs, contact, more):
                keep = True
                source = ((contact.get("properties") or {}).get("crm_source") or "").lower()
                if source in {"fireflies", "cube_acr", "allo"}:
                    promote = STAGE["discovery_completed"]
                elif source in {"calendly"} or hs.contact_has_meetings(contact["id"]):
                    promote = promote or STAGE["discovery_scheduled"]
                else:
                    promote = promote or STAGE["discovery_completed"]
        if keep and promote:
            hs.move_deal(deal_id, promote, evidence="prune:meeting-evidence")
            report.deals_moved.append(f"prune {name} -> {promote}")
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
