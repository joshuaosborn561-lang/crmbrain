from __future__ import annotations

import re
from datetime import datetime, timezone

from crmbrain.config import (
    CDT,
    JOSH_EMAILS,
    STAGE,
    Settings,
    gmail_after_clause,
    is_josh_address,
    is_non_deal_person,
    is_personal,
    is_zoom_room_address,
    settings_lookback_start,
)
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

NOTETAKER_DOMAINS = frozenset(
    {
        "fireflies.ai",
        "otter.ai",
        "fathom.video",
        "read.ai",
        "krisp.ai",
        "tldv.io",
    }
)


def mail_queries(settings: Settings) -> list[str]:
    after = gmail_after_clause(settings_lookback_start(settings))
    return [
        f"{after} (from:pandadoc.com OR from:e.pandadoc.com OR subject:PandaDoc)",
        f'{after} ("You received a payment" OR from:stripe.com OR from:quickbooks OR subject:payment received)',
        f'{after} (from:calendly.com ("New Event" OR Accepted OR canceled OR "no-show" OR "Invitee"))',
        f'{after} (from:zoom.us OR from:calendar-notification@google.com) (invitation OR confirmed OR scheduled OR "new event")',
        f"{after} (from:docusign.net OR subject:DocuSign completed)",
    ]


def people_queries(settings: Settings) -> tuple[str, ...]:
    after = gmail_after_clause(settings_lookback_start(settings))
    return (
        f"{after} in:sent -from:calendly.com -from:pandadoc.com -from:docusign.net",
        f"{after} in:inbox -category:promotions -from:calendly.com -from:noreply",
    )


MAX_GMAIL_PEOPLE = 80
GMAIL_PEOPLE_SEARCH_MAX = 80

SYSTEM_EMAIL_HINTS = (
    "salesglider",
    "pandadoc",
    "calendly",
    "docusign",
    "zoom.us",
    "stripe.com",
    "intuit.com",
    "fireflies.ai",
)
NOREPLY_HINTS = (
    "noreply",
    "no-reply",
    "donotreply",
    "mailer-daemon",
    "notifications@",
    "calendar-notification",
    "calendar-noreply",
    "no_reply",
    "@calendar.google.com",
)
PEOPLE_QUERIES = (
    "newer_than:2d in:sent -from:calendly.com -from:pandadoc.com -from:docusign.net",
    "newer_than:2d in:inbox -category:promotions -from:calendly.com -from:noreply",
)


def _addresses(header_value: str) -> list[str]:
    return [a.lower() for a in re.findall(r"[\w.+-]+@[\w.-]+", header_value or "")]


def _stage_from_mail(subject: str, sender: str, snippet: str, body: str = "") -> str:
    from crmbrain.documents import stage_from_signature_mail

    blob = f"{subject} {sender} {snippet} {body}".lower()
    if "you received a payment" in blob or "payment received" in blob:
        return STAGE["paid"]
    if "pandadoc" in blob or "docusign" in blob:
        stage, _amount, _name = stage_from_signature_mail(subject, sender, snippet, body)
        return stage
    if any(h in blob for h in ("calendly", "calendar-notification", "zoom.us")) and any(
        w in blob for w in ("canceled", "cancelled", "no-show", "no show")
    ):
        return STAGE["no_show"]
    if any(h in blob for h in ("calendly", "calendar-notification", "zoom.us")) and any(
        w in blob for w in ("new event", "accepted", "confirmed", "invitee", "invitation", "scheduled")
    ):
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
    if email and is_system_address(email):
        email = ""
    if not email:
        emails = [e for e in _addresses(text) if not is_system_address(e)]
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


def is_invite_notification(sender: str, subject: str) -> bool:
    blob = f"{sender} {subject}".lower()
    return any(
        h in blob
        for h in ("calendly", "calendar-notification", "zoom.us", "zoom.com", "filename:ics")
    )


def is_billing_or_signature_mail(sender: str, subject: str) -> bool:
    blob = f"{sender} {subject}".lower()
    return any(
        h in blob
        for h in ("pandadoc", "stripe.com", "docusign", "quickbooks", "intuit.com", "you received a payment")
    )


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
    for query in mail_queries(settings):
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
            invite_mail = is_invite_notification(sender, subject) and not is_billing_or_signature_mail(
                sender, subject
            )
            ics_emails: list[str] = []
            gcal_create = False
            classified = None
            if invite_mail:
                try:
                    from crmbrain.calendar_events import (
                        attendees_from_ics,
                        classify_ics,
                        may_create_contact_from_event,
                    )

                    for ics in gmail.calendar_parts(msg):
                        ics_emails.extend(attendees_from_ics(ics))
                        classified = classify_ics(ics)
                except Exception:
                    ics_emails = []
            header_emails = real_person_emails(
                [cal.get("email")] if cal.get("email") else [],
                _addresses(sender),
                _addresses(to),
            )
            invite_emails = real_person_emails(ics_emails, _addresses(body)) if invite_mail else []
            emails = real_person_emails(header_emails, invite_emails)
            contact = None
            for email in emails:
                contact = hubspot.find_contact(email=email)
                if contact:
                    break
            stage = _stage_from_mail(subject, sender, snippet, body)
            from crmbrain.documents import stage_from_signature_mail

            sig_stage, sig_amount, sig_name = stage_from_signature_mail(subject, sender, snippet, body)
            ev_email = cal.get("email") or (emails[0] if emails else "")
            calendly_create = (
                (not contact)
                and "calendly" in f"{sender} {subject}".lower()
                and is_josh_meeting(subject, cal.get("event_type", ""))
                and stage in {STAGE["discovery_scheduled"], STAGE["no_show"]}
            )
            if classified is not None and not contact and "calendly" not in f"{sender} {subject}".lower():
                gcal_create = may_create_contact_from_event(classified)
                if gcal_create and classified.primary_prospect:
                    ev_email = classified.primary_prospect
            create_new = calendly_create or gcal_create
            if create_new and (not ev_email or is_system_address(ev_email)):
                report.junk_blocked.append(f"gmail {subject[:80]} (system address)")
                continue
            if not contact and not create_new:
                from crmbrain.documents import commerce_match_fields, is_payment_mail

                payer, company, amount = commerce_match_fields(subject, snippet, body)
                amount = sig_amount or amount
                finder = getattr(hubspot, "find_contact_for_commerce", None)
                if callable(finder) and (is_payment_mail(subject, sender, snippet) or sig_stage or payer or company):
                    contact = finder(name=payer, company=company, amount=amount)
                if not contact:
                    report.junk_blocked.append(f"gmail {subject[:80]} (not in CRM)")
                    continue
            props = (contact or {}).get("properties") or {}
            if gcal_create and classified is not None:
                from crmbrain.names import person_name_from_attendee

                first, last = person_name_from_attendee("", ev_email)
                domain = ev_email.split("@")[1] if "@" in ev_email else ""
                extra_event = classified.title
            else:
                first = cal.get("first_name") or props.get("firstname") or ""
                last = cal.get("last_name") or props.get("lastname") or ""
                domain = cal.get("domain") or ""
                extra_event = cal.get("event_type", "")
            out.append(
                Engagement(
                    source="gmail",
                    external_id=mid,
                    occurred_at=datetime.fromtimestamp(int(msg.get("internalDate", "0")) / 1000, tz=timezone.utc),
                    email=ev_email or props.get("email") or "",
                    first_name=first,
                    last_name=last,
                    name=cal.get("name") or "",
                    company=cal.get("company") or props.get("company") or "",
                    domain=domain,
                    raw_subject=subject,
                    summary=f"{snippet}\n{cal.get('when') or ''}\n{extra_event}".strip(),
                    stage_hint=stage or sig_stage or (STAGE["discovery_scheduled"] if gcal_create else ""),
                    extra={
                        "hubspot_contact_id": contact["id"] if contact else "",
                        "from": sender,
                        "create_new": create_new,
                        "event_type": extra_event,
                        "meeting_when": cal.get("when", ""),
                        "meeting_at": cal.get("meeting_at", ""),
                        "gcal_create": gcal_create,
                        "amount": sig_amount,
                        "document_name": sig_name,
                    },
                )
            )
    return out


def is_system_address(email: str) -> bool:
    low = (email or "").strip().lower()
    if not low or low in JOSH_EMAILS:
        return True
    if any(h in low for h in SYSTEM_EMAIL_HINTS) or any(h in low for h in NOREPLY_HINTS):
        return True
    if is_zoom_room_address(low):
        return True
    local, _, domain = low.partition("@")
    if domain in {"calendar.google.com", "googlemail.com"}:
        return True
    if domain == "google.com" and any(
        tok in local for tok in ("calendar", "noreply", "no-reply", "notification", "invite")
    ):
        return True
    if local.startswith("noreply") or local.startswith("no-reply") or local.startswith("donotreply"):
        return True
    if "calendar-notification" in local:
        return True
    return False


def is_notetaker_email(email: str) -> bool:
    """Fireflies / Otter / Fathom style bots. Always archive, any deal stage."""
    low = (email or "").strip().lower()
    if not low or "@" not in low:
        return False
    domain = low.rsplit("@", 1)[-1]
    if domain in NOTETAKER_DOMAINS:
        return True
    if "notetaker" in low:
        return True
    return False


def is_notetaker_contact(contact: dict) -> bool:
    props = contact.get("properties") or {}
    email = (props.get("email") or "").strip()
    if is_notetaker_email(email):
        return True
    name = f"{props.get('firstname') or ''} {props.get('lastname') or ''}".lower()
    if "notetaker" in name:
        return True
    return False


def is_junk_crm_email(email: str) -> bool:
    """System/noreply calendar addresses that must never become HubSpot contacts.

    Empty email is not junk (Cube/phone-only meetings still need a path).
    """
    low = (email or "").strip().lower()
    if not low:
        return False
    return is_system_address(low) or is_notetaker_email(low)


def real_person_emails(*groups: list[str] | tuple[str, ...]) -> list[str]:
    out: list[str] = []
    for group in groups:
        for raw in group or []:
            email = (raw or "").strip().lower()
            if not email or is_system_address(email) or email in out:
                continue
            out.append(email)
    return out


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
    if from_email and not is_josh_address(from_email) and not is_system_address(from_email):
        return from_first, from_last, from_email
    for header in (to, cc):
        first, last, email = parse_person_header(header)
        if email and not is_josh_address(email) and not is_system_address(email):
            return first, last, email
    return "", "", ""


def _overflow_engagement(row: dict) -> Engagement:
    occurred = None
    raw = row.get("occurred_at")
    if raw:
        try:
            occurred = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            occurred = None
        if occurred and occurred.tzinfo is None:
            occurred = occurred.replace(tzinfo=timezone.utc)
    extra = dict(row.get("extra") or {})
    extra["skip_lookback"] = True
    extra["gmail_overflow"] = True
    return Engagement(
        source="gmail_person",
        external_id=str(row.get("external_id") or ""),
        occurred_at=occurred,
        email=str(row.get("email") or ""),
        first_name=str(row.get("first_name") or ""),
        last_name=str(row.get("last_name") or ""),
        name=str(row.get("name") or ""),
        domain=str(row.get("domain") or ""),
        company=str(row.get("company") or ""),
        raw_subject=str(row.get("raw_subject") or ""),
        summary=str(row.get("summary") or ""),
        extra=extra,
    )


def _overflow_row(ev: Engagement) -> dict:
    return {
        "external_id": ev.external_id,
        "email": ev.email,
        "first_name": ev.first_name,
        "last_name": ev.last_name,
        "name": ev.name,
        "domain": ev.domain,
        "company": ev.company,
        "raw_subject": ev.raw_subject,
        "summary": ev.summary,
        "occurred_at": ev.occurred_at.isoformat() if ev.occurred_at else None,
        "extra": ev.extra or {},
    }


def _people_rank(ev: Engagement, hubspot, known_emails: set[str]) -> tuple:
    email = (ev.email or "").strip().lower()
    known = 1 if email and email in known_emails else 0
    overflow = 1 if (ev.extra or {}).get("gmail_overflow") or (ev.extra or {}).get("skip_lookback") else 0
    in_crm = 0
    has_deal = 0
    has_meeting = known
    if hubspot and email and hasattr(hubspot, "find_contact"):
        try:
            contact = hubspot.find_contact(email=email)
        except Exception:
            contact = None
        if contact:
            in_crm = 1
            cid = contact.get("id")
            if cid and hasattr(hubspot, "open_deals_for_contact"):
                try:
                    has_deal = 1 if hubspot.open_deals_for_contact(cid) else 0
                except Exception:
                    has_deal = 0
            if hasattr(hubspot, "contact_has_meetings") and cid:
                try:
                    if hubspot.contact_has_meetings(cid):
                        has_meeting = 1
                except Exception:
                    pass
    occurred = ev.occurred_at or datetime.min.replace(tzinfo=timezone.utc)
    return (has_deal, has_meeting, in_crm, overflow, occurred)


def scan_people(
    settings: Settings,
    gmail: Gmail,
    hubspot=None,
    memory=None,
    known_emails: set[str] | None = None,
    report=None,
) -> list[Engagement]:
    """Josh emailed someone, or a real person emailed Josh. That is engagement.

    Read Sent and Inbox fully, then keep HubSpot-known / meeting people first.
    Overflow is persisted for the next run with skip_lookback so lookback cannot drop it.
    """
    seen: set[str] = set()
    seen_emails: set[str] = set()
    candidates: list[Engagement] = []
    known = {e.strip().lower() for e in (known_emails or set()) if e}
    if hubspot:
        known |= {e.lower() for e in (getattr(hubspot, "scheduled_attendee_emails", None) or set())}
        known |= {e.lower() for e in (getattr(hubspot, "recent_attendee_emails", None) or set())}

    if memory and hasattr(memory, "get_gmail_people_overflow"):
        for row in memory.get_gmail_people_overflow():
            ev = _overflow_engagement(row)
            email = (ev.email or "").strip().lower()
            if not email or is_josh_address(email) or is_system_address(email) or email in seen_emails:
                continue
            if ev.external_id:
                seen.add(ev.external_id)
            seen_emails.add(email)
            candidates.append(ev)

    for query in people_queries(settings):
        for stub in gmail.search(query, max_results=GMAIL_PEOPLE_SEARCH_MAX):
            mid = stub["id"]
            if mid in seen:
                continue
            seen.add(mid)
            try:
                msg = gmail.get(mid)
            except Exception as exc:
                if report is not None:
                    report.skipped.append(f"gmail_person:{mid} {exc}")
                    report.warnings.append(f"gmail_person skipped {mid}")
                continue
            headers = gmail.headers_map(msg)
            first, last, email = counterpart_from_headers(
                headers.get("from", ""),
                headers.get("to", ""),
                headers.get("cc", ""),
            )
            email = (email or "").strip().lower()
            if not email or is_josh_address(email) or is_system_address(email):
                continue
            if email in seen_emails:
                continue
            if is_personal(name=f"{first} {last}", email=email):
                continue
            if is_non_deal_person(name=f"{first} {last}", email=email):
                continue
            seen_emails.add(email)
            domain = email.split("@")[1] if "@" in email else ""
            candidates.append(
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

    ranked = sorted(candidates, key=lambda ev: _people_rank(ev, hubspot, known), reverse=True)
    selected = ranked[:MAX_GMAIL_PEOPLE]
    overflow = ranked[MAX_GMAIL_PEOPLE:]
    if memory and hasattr(memory, "upsert_gmail_people_overflow"):
        memory.upsert_gmail_people_overflow([_overflow_row(ev) for ev in overflow])
    elif memory and hasattr(memory, "set_gmail_people_overflow"):
        memory.set_gmail_people_overflow([_overflow_row(ev) for ev in overflow])
    if report is not None:
        report.gmail_people_overflow = len(overflow)
        if overflow:
            report.warnings.append(f"gmail_person overflow: {len(overflow)}")
    return selected
