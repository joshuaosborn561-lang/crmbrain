"""Upcoming / recent Google Calendar + Calendly attendees.

Direct GCal invites never created a HubSpot meeting engagement and were not
treated as Calendly bookings, so booked prospects were pruned as 'no meeting'.

Upcoming events can promote Discovery Scheduled. Past events may only keep
an existing contact from being pruned.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable

from crmbrain.config import (
    JOSH_DOMAINS,
    NON_SALES_TITLE_HINTS,
    Settings,
    is_internal_meeting,
    is_josh_address as email_is_josh,
    now_utc,
)
from crmbrain.gmail_client import Gmail
from crmbrain.models import Engagement
from crmbrain.names import person_name_from_attendee
from crmbrain.sources.gmail_scan import is_notetaker_email, is_system_address

logger = logging.getLogger(__name__)

RECENT_DAYS = 7
UPCOMING_DAYS = 30
ATTENDEE_RE = re.compile(
    r"^ATTENDEE([^\n]*):(?:mailto:)?([^\s\n>]+)",
    re.I | re.M,
)
ORGANIZER_RE = re.compile(
    r"^ORGANIZER([^\n]*):(?:mailto:)?([^\s\n>]+)",
    re.I | re.M,
)
DTSTART_RE = re.compile(r"^DTSTART([^:]*):([^\s\n]+)", re.I | re.M)
STATUS_RE = re.compile(r"^STATUS:([^\s\n]+)", re.I | re.M)
SUMMARY_RE = re.compile(r"^SUMMARY:([^\n]+)", re.I | re.M)
DESCRIPTION_RE = re.compile(r"^DESCRIPTION:([^\n]+)", re.I | re.M)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+")
SALES_MEETING_HINTS = (
    "salesglider",
    "sg intro",
    "intro",
    "discovery",
    "disco",
    "demo",
    "cold email",
    "kickoff",
    "onboarding",
    "poc",
    "pilot",
    "proposal",
    "call with josh",
    "call with joshua",
)


@dataclass
class ClassifiedEvent:
    title: str = ""
    description: str = ""
    start: datetime | None = None
    all_day: bool = False
    cancelled: bool = False
    josh_declined: bool = False
    josh_organized: bool = False
    josh_accepted: bool = False
    upcoming: bool = False
    external_attendees: list[str] = field(default_factory=list)
    primary_prospect: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class CalendarSnapshot:
    upcoming: set[str] = field(default_factory=set)
    recent: set[str] = field(default_factory=set)
    create_engagements: list[Engagement] = field(default_factory=list)
    events: list[ClassifiedEvent] = field(default_factory=list)
    calendar_api_ok: bool = True
    calendar_api_error: str = ""

    def protect_emails(self) -> set[str]:
        return set(self.upcoming) | set(self.recent)


def event_window(now: datetime | None = None, upcoming_days: int = UPCOMING_DAYS) -> tuple[datetime, datetime]:
    now = now or now_utc()
    return now - timedelta(days=RECENT_DAYS), now + timedelta(days=upcoming_days)


def is_josh_address(email: str) -> bool:
    low = (email or "").strip().lower()
    if not low:
        return False
    if email_is_josh(low) or is_system_address(low):
        return True
    domain = low.rsplit("@", 1)[-1] if "@" in low else ""
    return domain in JOSH_DOMAINS


def is_calendar_resource_address(email: str) -> bool:
    low = (email or "").strip().lower()
    if not low or "@" not in low:
        return False
    domain = low.rsplit("@", 1)[-1]
    return domain == "calendar.google.com" or domain.endswith(".calendar.google.com")


def is_excluded_attendee(email: str) -> bool:
    low = (email or "").strip().lower()
    if not low:
        return True
    if is_josh_address(low) or is_notetaker_email(low) or is_calendar_resource_address(low):
        return True
    return is_system_address(low)


def looks_like_non_sales_meeting(title: str, description: str = "") -> bool:
    blob = f"{title} {description}".lower()
    return any(hint in blob for hint in NON_SALES_TITLE_HINTS)


def looks_like_sales_meeting(
    title: str, description: str = "", *, josh_one_on_one: bool = False
) -> bool:
    """Legacy title-keyword helper. Create gating uses the intent classifier."""
    del josh_one_on_one
    if looks_like_non_sales_meeting(title, description):
        return False
    blob = f"{title} {description}".lower()
    return any(hint in blob for hint in SALES_MEETING_HINTS)


def primary_prospect(emails: Iterable[str], title: str = "") -> str:
    people = []
    seen = set()
    for raw in emails:
        email = (raw or "").strip().lower()
        if not email or email in seen or is_excluded_attendee(email):
            continue
        seen.add(email)
        people.append(email)
    if not people:
        return ""
    if len(people) == 1:
        return people[0]
    domains = {e.rsplit("@", 1)[-1] for e in people}
    if len(domains) != 1:
        return ""
    title_l = (title or "").lower()
    for email in people:
        local = email.split("@")[0]
        if local and local in title_l:
            return email
        first, last = person_name_from_attendee("", email)
        if first and first.lower() in title_l:
            return email
        if last and last.lower() in title_l:
            return email
    return people[0]


def attendees_from_ics(ics: str) -> set[str]:
    emails: set[str] = set()
    for pattern in (ATTENDEE_RE, ORGANIZER_RE):
        for _params, email in pattern.findall(ics or ""):
            email = (email or "").strip().rstrip(".").lower()
            if email:
                emails.add(email)
    return {e for e in emails if not is_excluded_attendee(e)}


def attendees_from_text(*chunks: str) -> set[str]:
    found: set[str] = set()
    for chunk in chunks:
        found.update(EMAIL_RE.findall(chunk or ""))
    return {e.lower() for e in found if not is_excluded_attendee(e)}


def _parse_ics_dt(raw: str, params: str) -> tuple[datetime | None, bool]:
    value = (raw or "").strip()
    params_l = (params or "").lower()
    if not value:
        return None, False
    if "value=date" in params_l or ("t" not in value.lower() and len(value) == 8):
        try:
            day = datetime.strptime(value[:8], "%Y%m%d").replace(tzinfo=timezone.utc)
            return day, True
        except ValueError:
            return None, True
    compact = value.replace("Z", "").replace("-", "")
    if "T" in compact:
        compact = compact.split("+")[0]
    for fmt, size in (("%Y%m%dT%H%M%S", 15), ("%Y%m%dT%H%M", 13)):
        try:
            naive = datetime.strptime(compact[:size], fmt)
            return naive.replace(tzinfo=timezone.utc), False
        except ValueError:
            continue
    return None, False


def classify_ics(ics: str, now: datetime | None = None) -> ClassifiedEvent:
    now = now or now_utc()
    ev = ClassifiedEvent()
    summary = SUMMARY_RE.search(ics or "")
    ev.title = summary.group(1).strip() if summary else ""
    desc = DESCRIPTION_RE.search(ics or "")
    ev.description = desc.group(1).strip() if desc else ""
    status_m = STATUS_RE.search(ics or "")
    status = status_m.group(1).lower() if status_m else ""
    ev.cancelled = status == "cancelled"
    start_m = DTSTART_RE.search(ics or "")
    if start_m:
        ev.start, ev.all_day = _parse_ics_dt(start_m.group(2), start_m.group(1))
    if ev.start:
        ev.upcoming = ev.start > now and not ev.all_day
    organizer = ""
    org_m = ORGANIZER_RE.search(ics or "")
    if org_m:
        organizer = org_m.group(2).strip().lower()
        ev.josh_organized = is_josh_address(organizer)
    externals: list[str] = []
    for params, email in ATTENDEE_RE.findall(ics or ""):
        email = (email or "").strip().rstrip(".").lower()
        params_l = (params or "").lower()
        if is_josh_address(email):
            if "partstat=declined" in params_l:
                ev.josh_declined = True
            if "partstat=accepted" in params_l:
                ev.josh_accepted = True
            continue
        if "partstat=declined" in params_l:
            continue
        if is_excluded_attendee(email):
            continue
        if email not in externals:
            externals.append(email)
    if ev.josh_organized and not ev.josh_declined:
        ev.josh_accepted = ev.josh_accepted or True
    ev.external_attendees = externals
    ev.primary_prospect = primary_prospect(externals, ev.title)
    return ev


def _event_start(event: dict) -> tuple[datetime | None, bool]:
    start = event.get("start") or {}
    if start.get("date") and not start.get("dateTime"):
        try:
            day = datetime.strptime(str(start["date"])[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            return day, True
        except ValueError:
            return None, True
    raw = start.get("dateTime") or ""
    if not raw:
        return None, False
    try:
        stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp, False
    except ValueError:
        return None, False


def classify_gcal_event(event: dict, now: datetime | None = None) -> ClassifiedEvent:
    now = now or now_utc()
    ev = ClassifiedEvent(
        title=event.get("summary") or event.get("title") or "",
        description=event.get("description") or "",
        cancelled=(event.get("status") or "").lower() == "cancelled",
        extra={"event_id": event.get("id") or ""},
    )
    ev.start, ev.all_day = _event_start(event)
    if ev.start:
        ev.upcoming = ev.start > now and not ev.all_day
    organizer = ((event.get("organizer") or {}).get("email") or "").strip().lower()
    ev.josh_organized = is_josh_address(organizer)
    externals: list[str] = []
    for attendee in event.get("attendees") or []:
        email = (attendee.get("email") or "").strip().lower()
        status = (attendee.get("responseStatus") or "").lower()
        if attendee.get("resource") or attendee.get("self"):
            if attendee.get("self") and is_josh_address(email) and status == "declined":
                ev.josh_declined = True
            if attendee.get("self") and is_josh_address(email) and status == "accepted":
                ev.josh_accepted = True
            continue
        if is_josh_address(email):
            if status == "declined":
                ev.josh_declined = True
            if status == "accepted":
                ev.josh_accepted = True
            continue
        if status == "declined":
            continue
        if is_excluded_attendee(email):
            continue
        if email and email not in externals:
            externals.append(email)
    if ev.josh_organized and not ev.josh_declined:
        ev.josh_accepted = True
    ev.external_attendees = externals
    ev.primary_prospect = primary_prospect(externals, ev.title)
    return ev


def usable_event(ev: ClassifiedEvent) -> bool:
    if ev.cancelled or ev.all_day or ev.josh_declined:
        return False
    if not ev.external_attendees:
        return False
    if is_internal_meeting(ev.title, ev.external_attendees):
        return False
    return True


def passes_calendar_create_gate(ev: ClassifiedEvent) -> bool:
    """PR #13 structural gate: upcoming, Josh org/accepted, not all-day, one prospect."""
    if not usable_event(ev):
        return False
    if not ev.upcoming:
        return False
    if not (ev.josh_organized or ev.josh_accepted):
        return False
    if not ev.primary_prospect:
        return False
    return True


def _intent_for_classified(ev: ClassifiedEvent, settings: Settings | None = None):
    from crmbrain.intent import classify

    return classify(settings, _engagement_from_event(ev, "calendar-intent"))


def may_create_contact_from_event(ev: ClassifiedEvent, settings: Settings | None = None) -> bool:
    """Create only when the structural gate passes AND intent is a sales opportunity."""
    if not passes_calendar_create_gate(ev):
        return False
    from crmbrain.intent import is_confident_sales

    min_c = float(getattr(settings, "intent_min_confidence", 0.75) or 0.75)
    decision = _intent_for_classified(ev, settings)
    return is_confident_sales(decision, min_c)


def attendees_from_gcal_event(event: dict) -> set[str]:
    classified = classify_gcal_event(event)
    if not usable_event(classified):
        return set()
    return set(classified.external_attendees)


def _event_title(event: dict) -> str:
    return event.get("summary") or event.get("title") or ""


def keep_event_attendees(title: str, emails: Iterable[str]) -> set[str]:
    people = {e.lower() for e in emails if e and not is_excluded_attendee(e)}
    if not people:
        return set()
    if is_internal_meeting(title, list(people)):
        return set()
    return people


def _engagement_from_event(ev: ClassifiedEvent, external_id: str) -> Engagement:
    first, last = person_name_from_attendee("", ev.primary_prospect)
    domain = ev.primary_prospect.split("@")[1] if "@" in ev.primary_prospect else ""
    return Engagement(
        source="calendly",
        external_id=external_id,
        occurred_at=ev.start,
        email=ev.primary_prospect,
        first_name=first,
        last_name=last,
        name=f"{first} {last}".strip(),
        domain=domain,
        company=domain.split(".")[0].replace("-", " ").title() if domain else "",
        raw_subject=ev.title,
        stage_hint="qualifiedtobuy",
        extra={
            "create_new": True,
            "event_type": ev.title,
            "meeting_at": ev.start.isoformat() if ev.start else "",
            "gcal_create": True,
        },
    )


def load_calendar(gmail: Gmail, settings: Settings | None = None) -> CalendarSnapshot:
    upcoming_days = getattr(settings, "calendar_upcoming_days", None) or UPCOMING_DAYS
    start, end = event_window(upcoming_days=upcoming_days)
    snap = CalendarSnapshot()
    try:
        events = gmail.list_calendar_events(start, end)
        snap.calendar_api_ok = True
        for event in events:
            classified = classify_gcal_event(event)
            snap.events.append(classified)
            if not usable_event(classified):
                continue
            emails = set(classified.external_attendees)
            if classified.upcoming:
                snap.upcoming.update(emails)
                if passes_calendar_create_gate(classified):
                    eid = str((event.get("id") or classified.primary_prospect) or "")
                    if eid:
                        snap.create_engagements.append(_engagement_from_event(classified, f"gcal:{eid}"))
            else:
                snap.recent.update(emails)
    except Exception as exc:
        snap.calendar_api_ok = False
        snap.calendar_api_error = str(exc)
        logger.warning("calendar api attendees skipped: %s", exc)
    try:
        gmail_snap = _snapshot_from_gmail_invites(gmail, start)
        snap.upcoming.update(gmail_snap.upcoming)
        snap.recent.update(gmail_snap.recent)
        snap.create_engagements.extend(gmail_snap.create_engagements)
        snap.events.extend(gmail_snap.events)
    except Exception as exc:
        logger.warning("gmail invite attendees skipped: %s", exc)
    return snap


def load_attendee_emails(gmail: Gmail, settings: Settings | None = None) -> set[str]:
    return load_calendar(gmail, settings).protect_emails()


def _snapshot_from_gmail_invites(gmail: Gmail, start: datetime) -> CalendarSnapshot:
    after = start.astimezone(timezone.utc).strftime("%Y/%m/%d")
    query = (
        f"after:{after} "
        "(from:calendar-notification@google.com OR from:calendly.com OR filename:ics) "
        "(invitation OR invite OR invited OR accepted OR confirmed OR scheduled "
        'OR "new event" OR "updated event")'
    )
    snap = CalendarSnapshot()
    for stub in gmail.search(query, max_results=40):
        msg = gmail.get(stub["id"])
        headers = gmail.headers_map(msg)
        subject = headers.get("subject", "")
        body = gmail.body_text(msg)
        classified = ClassifiedEvent(title=subject, description=body)
        for ics in gmail.calendar_parts(msg):
            parsed = classify_ics(ics)
            if parsed.title:
                classified = parsed
                break
        snap.events.append(classified)
        if classified.start is None:
            people = attendees_from_text(body)
            for ics in gmail.calendar_parts(msg):
                people.update(attendees_from_ics(ics))
            people = keep_event_attendees(subject, people)
            if people and not any(w in f"{subject}\n{body}".lower() for w in ("canceled", "cancelled")):
                snap.recent.update(people)
            continue
        if not usable_event(classified):
            continue
        emails = set(classified.external_attendees)
        if classified.upcoming:
            snap.upcoming.update(emails)
            if passes_calendar_create_gate(classified):
                eid = f"gmail-ics:{stub['id']}:{classified.primary_prospect}"
                snap.create_engagements.append(_engagement_from_event(classified, eid))
        else:
            snap.recent.update(emails)
    return snap


def contact_on_calendar(email: str, attendee_emails: Iterable[str]) -> bool:
    low = (email or "").strip().lower()
    if not low:
        return False
    return low in {e.lower() for e in attendee_emails}
