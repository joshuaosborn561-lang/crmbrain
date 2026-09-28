"""Upcoming / recent Google Calendar + Calendly attendees.

Direct GCal invites never created a HubSpot meeting engagement and were not
treated as Calendly bookings, so booked prospects were pruned as 'no meeting'.
Any non-cancelled event where the contact is an attendee counts as scheduled.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Iterable

from crmbrain.config import Settings, is_internal_meeting, now_utc
from crmbrain.gmail_client import Gmail
from crmbrain.sources.gmail_scan import is_system_address, real_person_emails

logger = logging.getLogger(__name__)

RECENT_DAYS = 7
UPCOMING_DAYS = 45
ATTENDEE_RE = re.compile(
    r"ATTENDEE[^\n]*?(?:CN=([^;:\n]+))?[^\n]*?:mailto:([^\s\n>]+)",
    re.I,
)
ORGANIZER_RE = re.compile(
    r"ORGANIZER[^\n]*?(?:CN=([^;:\n]+))?[^\n]*?:mailto:([^\s\n>]+)",
    re.I,
)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+")


def event_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    now = now or now_utc()
    return now - timedelta(days=RECENT_DAYS), now + timedelta(days=UPCOMING_DAYS)


def attendees_from_ics(ics: str) -> set[str]:
    emails: set[str] = set()
    for pattern in (ATTENDEE_RE, ORGANIZER_RE):
        for _cn, email in pattern.findall(ics or ""):
            email = (email or "").strip().rstrip(".").lower()
            if email:
                emails.add(email)
    return {e for e in emails if not is_system_address(e)}


def attendees_from_text(*chunks: str) -> set[str]:
    found: set[str] = set()
    for chunk in chunks:
        found.update(EMAIL_RE.findall(chunk or ""))
    return {e.lower() for e in real_person_emails(list(found))}


def attendees_from_gcal_event(event: dict) -> set[str]:
    if (event.get("status") or "").lower() == "cancelled":
        return set()
    emails: list[str] = []
    organizer = (event.get("organizer") or {}).get("email") or ""
    if organizer:
        emails.append(organizer)
    for attendee in event.get("attendees") or []:
        if (attendee.get("responseStatus") or "").lower() == "declined":
            continue
        email = attendee.get("email") or ""
        if email:
            emails.append(email)
    return {e.lower() for e in real_person_emails(emails)}


def _event_title(event: dict) -> str:
    return event.get("summary") or event.get("title") or ""


def keep_event_attendees(title: str, emails: Iterable[str]) -> set[str]:
    people = {e.lower() for e in emails if e and not is_system_address(e)}
    if not people:
        return set()
    if is_internal_meeting(title, list(people)):
        return set()
    return people


def load_attendee_emails(gmail: Gmail, settings: Settings | None = None) -> set[str]:
    """Union of Calendar API attendees and Gmail invite/ICS attendees.

    Missing calendar.readonly scope is not an error — Gmail invites still count.
    """
    del settings
    start, end = event_window()
    found: set[str] = set()
    try:
        for event in gmail.list_calendar_events(start, end):
            title = _event_title(event)
            found.update(keep_event_attendees(title, attendees_from_gcal_event(event)))
    except Exception as exc:
        logger.warning("calendar api attendees skipped: %s", exc)
    try:
        found.update(_attendees_from_gmail_invites(gmail, start))
    except Exception as exc:
        logger.warning("gmail invite attendees skipped: %s", exc)
    return found


def _attendees_from_gmail_invites(gmail: Gmail, start: datetime) -> set[str]:
    after = start.astimezone(timezone.utc).strftime("%Y/%m/%d")
    query = (
        f"after:{after} "
        "(from:calendar-notification@google.com OR from:calendly.com OR filename:ics) "
        "(invitation OR invite OR invited OR accepted OR confirmed OR scheduled "
        'OR "new event" OR "updated event")'
    )
    emails: set[str] = set()
    for stub in gmail.search(query, max_results=40):
        msg = gmail.get(stub["id"])
        headers = gmail.headers_map(msg)
        subject = headers.get("subject", "")
        body = gmail.body_text(msg)
        blob = f"{subject}\n{body}"
        if any(w in blob.lower() for w in ("canceled", "cancelled")):
            continue
        people = attendees_from_text(body)
        for ics in gmail.calendar_parts(msg):
            people.update(attendees_from_ics(ics))
        emails.update(keep_event_attendees(subject, people))
    return emails


def contact_on_calendar(email: str, attendee_emails: Iterable[str]) -> bool:
    low = (email or "").strip().lower()
    if not low:
        return False
    return low in {e.lower() for e in attendee_emails}
