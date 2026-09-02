from __future__ import annotations

import re
from datetime import datetime, timezone

from crmbrain.config import CDT, JOSH_EMAILS, STAGE, Settings, is_personal
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

SYSTEM_EMAIL_HINTS = (
    "salesglider",
    "pandadoc",
    "calendly",
    "docusign",
    "zoom.us",
    "stripe.com",
    "intuit.com",
)
NOREPLY_HINTS = (
    "noreply",
    "no-reply",
    "donotreply",
    "mailer-daemon",
    "notifications@",
    "calendar-notification",
    "no_reply",
)
PEOPLE_QUERIES = (
    "newer_than:2d in:sent -from:calendly.com -from:pandadoc.com -from:docusign.net",
    "newer_than:2d in:inbox -category:promotions -from:calendly.com -from:noreply",
)


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


def parse_calendly(subject: str, body: str) -> dict[str, str]:
    text = f"{subject}\n{body}"
    invitee = _field(text, "Invitee")
    email = _field(text, "Invitee Email")
    event_type = _field(text, "Event Type")
    when = _field(text, "Event Date/Time") or _field(text, "Event Date/Time:")
    if not invitee:
        m = re.search(r"New Event:\s*(.+?)\s+-\s+\d", subject)
        if m:
            invitee = m.group(1).strip()
    if not email:
        emails = [e for e in _addresses(text) if not any(h in e for h in SYSTEM_EMAIL_HINTS)]
        email = emails[0] if emails else ""
    first, last = "", ""
    if invitee:
        parts = invitee.split()
        first, last = parts[0], " ".join(parts[1:])
    domain = email.split("@")[1] if "@" in email else ""
    meeting_at = parse_meeting_at(subject, body)
    return {
        "name": invitee,
        "first_name": first,
        "last_name": last,
        "email": email,
        "event_type": event_type,
        "when": when or (meeting_at.astimezone(CDT).strftime("%a %b %-d %Y %-I:%M%p %Z") if meeting_at else ""),
        "domain": domain,
        "company": _company_from_domain(domain),
        "meeting_at": meeting_at.isoformat() if meeting_at else "",
    }


SUBJECT_WHEN = re.compile(
    r"(\d{1,2}:\d{2}\s*[ap]m)\s+\w{3},?\s+(\w{3})\s+(\d{1,2}),?\s+(\d{4})",
    re.I,
)
BODY_WHEN = re.compile(
    r"(\w+day),?\s+(\w+)\s+(\d{1,2}),?\s+(\d{4})\s+at\s+(\d{1,2}:\d{2}\s*[ap]m)",
    re.I,
)


def parse_meeting_at(subject: str, body: str = "") -> datetime | None:
    """Calendly times are Josh's Chicago clock."""
    text = f"{subject}\n{body}"
    m = SUBJECT_WHEN.search(subject) or SUBJECT_WHEN.search(text)
    if m:
        stamp = _parse_clock(m.group(1), m.group(2), m.group(3), m.group(4))
        if stamp:
            return stamp
    m = BODY_WHEN.search(text)
    if m:
        stamp = _parse_clock(m.group(5), m.group(2), m.group(3), m.group(4))
        if stamp:
            return stamp
    return None


def _parse_clock(time_part: str, month: str, day: str, year: str) -> datetime | None:
    clock = re.sub(r"\s+", "", time_part).upper()
    month = month[:3].title()
    for fmt in ("%I:%M%p %b %d %Y", "%I:%M%p %B %d %Y"):
        try:
            naive = datetime.strptime(f"{clock} {month} {int(day)} {year}", fmt)
            return naive.replace(tzinfo=CDT)
        except ValueError:
            continue
    return None


def is_josh_meeting(subject: str, event_type: str) -> bool:
    blob = f"{subject} {event_type}".lower()
    return "salesglider" in blob


def _field(text: str, label: str) -> str:
    m = re.search(rf"{re.escape(label)}:\s*([^\n<]+)", text, re.I)
    return (m.group(1).strip() if m else "")


def _company_from_domain(domain: str) -> str:
    if not domain:
        return ""
    host = domain.split(".")[0]
    return host.replace("-", " ").title()


def scan(settings: Settings, gmail: Gmail, hubspot: HubSpot, report: CycleReport) -> list[Engagement]:
    """Gmail updates existing CRM people. Josh Calendly bookings also create new ones."""
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
            body = gmail.body_text(msg)
            cal = parse_calendly(subject, body) if "calendly" in f"{sender} {subject}".lower() else {}
            emails = [
                e
                for e in ([cal.get("email")] if cal.get("email") else []) + _addresses(sender) + _addresses(to)
                if e and not any(h in e for h in SYSTEM_EMAIL_HINTS)
            ]
            contact = None
            for email in emails:
                contact = hubspot.find_contact(email=email)
                if contact:
                    break
            stage = _stage_from_mail(subject, sender, f"{snippet} {body}")
            create_new = (not contact) and is_josh_meeting(subject, cal.get("event_type", "")) and stage in {
                STAGE["discovery_scheduled"],
                STAGE["no_show"],
            }
            if not contact and not create_new:
                report.junk_blocked.append(f"gmail {subject[:80]} (not in CRM)")
                continue
            props = (contact or {}).get("properties") or {}
            out.append(
                Engagement(
                    source="gmail",
                    external_id=mid,
                    occurred_at=datetime.fromtimestamp(int(msg.get("internalDate", "0")) / 1000, tz=timezone.utc),
                    email=cal.get("email") or props.get("email") or (emails[0] if emails else ""),
                    first_name=cal.get("first_name") or props.get("firstname") or "",
                    last_name=cal.get("last_name") or props.get("lastname") or "",
                    name=cal.get("name") or "",
                    company=cal.get("company") or props.get("company") or "",
                    domain=cal.get("domain") or "",
                    raw_subject=subject,
                    summary=f"{snippet}\n{cal.get('when') or ''}\n{cal.get('event_type') or ''}".strip(),
                    stage_hint=stage,
                    extra={
                        "hubspot_contact_id": contact["id"] if contact else "",
                        "from": sender,
                        "create_new": create_new,
                        "event_type": cal.get("event_type", ""),
                        "meeting_when": cal.get("when", ""),
                    },
                )
            )
    return out


def is_system_address(email: str) -> bool:
    low = (email or "").lower()
    if not low or low in JOSH_EMAILS:
        return True
    if any(h in low for h in SYSTEM_EMAIL_HINTS) or any(h in low for h in NOREPLY_HINTS):
        return True
    return False


def parse_person_header(header: str) -> tuple[str, str, str]:
    """Return first, last, email from a From/To header."""
    header = header or ""
    email = (_addresses(header) or [""])[0]
    name = ""
    m = re.match(r"\s*\"?([^\"<]+?)\"?\s*<", header)
    if m:
        name = m.group(1).strip().strip("'")
    parts = [p for p in name.split() if p]
    first = parts[0] if parts else ""
    last = " ".join(parts[1:]) if len(parts) > 1 else ""
    return first, last, email


def counterpart_from_headers(sender: str, to: str, cc: str = "") -> tuple[str, str, str]:
    """The other person on a Josh email. Sent → To. Inbox → From."""
    from_first, from_last, from_email = parse_person_header(sender)
    if from_email and from_email.lower() not in JOSH_EMAILS and not is_system_address(from_email):
        return from_first, from_last, from_email
    for header in (to, cc):
        first, last, email = parse_person_header(header)
        if email and not is_system_address(email):
            return first, last, email
    return "", "", ""


def scan_people(settings: Settings, gmail: Gmail) -> list[Engagement]:
    """Josh emailed someone, or a real person emailed Josh. That is engagement."""
    seen: set[str] = set()
    out: list[Engagement] = []
    for query in PEOPLE_QUERIES:
        for stub in gmail.search(query, max_results=40):
            mid = stub["id"]
            if mid in seen:
                continue
            seen.add(mid)
            msg = gmail.get(mid)
            headers = gmail.headers_map(msg)
            first, last, email = counterpart_from_headers(
                headers.get("from", ""),
                headers.get("to", ""),
                headers.get("cc", ""),
            )
            if not email or is_personal(name=f"{first} {last}", email=email):
                continue
            domain = email.split("@")[1] if "@" in email else ""
            out.append(
                Engagement(
                    source="gmail_person",
                    external_id=mid,
                    occurred_at=datetime.fromtimestamp(int(msg.get("internalDate", "0")) / 1000, tz=timezone.utc),
                    email=email,
                    first_name=first,
                    last_name=last,
                    name=f"{first} {last}".strip(),
                    domain=domain,
                    company=_company_from_domain(domain),
                    raw_subject=headers.get("subject", ""),
                    summary=msg.get("snippet", "")[:400],
                )
            )
    return out
