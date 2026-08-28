from __future__ import annotations

import re
from datetime import datetime, timezone

from crmbrain.config import STAGE, Settings
from crmbrain.gmail_client import Gmail
from crmbrain.hubspot import HubSpot
from crmbrain.models import CycleReport, Engagement

QUERIES = [
    "newer_than:2d (from:pandadoc.com OR from:e.pandadoc.com OR subject:PandaDoc)",
    "newer_than:2d (\"You received a payment\" OR from:stripe.com OR from:quickbooks OR subject:payment received)",
    "newer_than:2d (from:calendly.com (\"New Event\" OR Accepted OR canceled OR \"no-show\" OR \"Invitee\"))",
    "newer_than:2d (from:zoom.us OR from:calendar-notification@google.com) (invitation OR confirmed OR scheduled OR \"new event\")",
    "newer_than:2d (from:docusign.net OR subject:DocuSign completed)",
]


def _addresses(header_value: str) -> list[str]:
    return [a.lower() for a in re.findall(r"[\w.+-]+@[\w.-]+", header_value or "")]


def _stage_from_mail(subject: str, sender: str, snippet: str) -> str:
    blob = f"{subject} {sender} {snippet}".lower()
    if "you received a payment" in blob or "payment received" in blob:
        return STAGE["paid"]
    if "pandadoc" in blob and any(w in blob for w in ("completed", "signed", "has been signed")):
        return STAGE["signed"]
    if "docusign" in blob and "completed" in blob:
        return STAGE["signed"]
    if "pandadoc" in blob and any(w in blob for w in ("sent you", "viewed", "document was sent")):
        return STAGE["proposal_sent"]
    if "calendly" in blob and any(w in blob for w in ("canceled", "cancelled", "no-show", "no show")):
        return STAGE["no_show"]
    if "calendly" in blob and any(w in blob for w in ("new event", "accepted", "confirmed", "invitee")):
        return STAGE["discovery_scheduled"]
    return ""


def scan(settings: Settings, gmail: Gmail, hubspot: HubSpot, report: CycleReport) -> list[Engagement]:
    """Gmail is gated: person must already be in HubSpot."""
    seen = set()
    out: list[Engagement] = []
    for query in QUERIES:
        for stub in gmail.search(query, max_results=30):
            mid = stub["id"]
            if mid in seen:
                continue
            seen.add(mid)
            msg = gmail.get(mid)
            headers = gmail.headers_map(msg)
            subject = headers.get("subject", "")
            sender = headers.get("from", "")
            to = headers.get("to", "")
            snippet = msg.get("snippet", "")
            emails = [e for e in _addresses(sender) + _addresses(to) if "salesglider" not in e and "pandadoc" not in e and "calendly" not in e and "docusign" not in e and "zoom.us" not in e]
            contact = None
            for email in emails:
                contact = hubspot.find_contact(email=email)
                if contact:
                    break
            if not contact:
                report.junk_blocked.append(f"gmail {subject[:80]} (not in CRM)")
                continue
            stage = _stage_from_mail(subject, sender, snippet)
            props = contact.get("properties") or {}
            out.append(
                Engagement(
                    source="gmail",
                    external_id=mid,
                    occurred_at=datetime.fromtimestamp(int(msg.get("internalDate", "0")) / 1000, tz=timezone.utc),
                    email=props.get("email") or (emails[0] if emails else ""),
                    first_name=props.get("firstname") or "",
                    last_name=props.get("lastname") or "",
                    company=props.get("company") or "",
                    raw_subject=subject,
                    summary=snippet,
                    stage_hint=stage,
                    extra={"hubspot_contact_id": contact["id"], "from": sender},
                )
            )
    return out
