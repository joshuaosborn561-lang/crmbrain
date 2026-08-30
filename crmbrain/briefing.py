from __future__ import annotations

from datetime import datetime, timedelta

from crmbrain.config import CDT, Settings, digits_phone, now_utc
from crmbrain.gmail_client import Gmail
from crmbrain.hubspot import HubSpot
from crmbrain.leadmagic import usable_linkedin
from crmbrain.memory import Memory
from crmbrain.models import CycleReport, Engagement
from crmbrain.sources.gmail_scan import is_josh_meeting, parse_calendly, parse_meeting_at

# One brief, about two hours before the call. Hourly cron hits this once.
LEAD_EARLY = timedelta(hours=2, minutes=30)
LEAD_LATE = timedelta(hours=1)


def due_to_send(meeting_at: datetime, now: datetime | None = None) -> bool:
    now = now or now_utc()
    if meeting_at.tzinfo is None:
        meeting_at = meeting_at.replace(tzinfo=now.tzinfo)
    delta = meeting_at - now
    return LEAD_LATE <= delta <= LEAD_EARLY


def brief_key(email: str, meeting_at: datetime) -> str:
    return f"{(email or '').lower()}|{meeting_at.astimezone(CDT).strftime('%Y%m%d%H%M')}"


def format_when(meeting_at: datetime) -> str:
    local = meeting_at.astimezone(CDT)
    hour = local.strftime("%I:%M%p").lstrip("0").lower()
    tz = "CDT" if local.dst() else "CST"
    return f"{local.strftime('%a %b %-d %Y')} {hour} {tz}"


def format_phone(value: str | None) -> str:
    digits = digits_phone(value)
    if len(digits) >= 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits[-10:] if len(digits) >= 10 else (value or "")


def default_offer(company: str = "", title: str = "", pain: str = "") -> str:
    blob = f"{company} {title} {pain}".lower()
    if any(w in blob for w in ("health", "medtech", "medical", "insurance", "device", "pharma")):
        return "Loom on a healthcare or MedTech owner list, or Airpods on a tight test campaign."
    if any(w in blob for w in ("recruit", "search", "staffing", "talent")):
        return "Loom on an owner list in their niche, or Airpods on a tight test campaign."
    return "Loom on their exact owner list, or Airpods on a tight test campaign."


def render(ev: Engagement, facts: dict | None = None, contact: dict | None = None) -> str:
    facts = facts or {}
    props = (contact or {}).get("properties") or {}
    name = ev.display_name() or f"{props.get('firstname') or ''} {props.get('lastname') or ''}".strip() or ev.email
    company = ev.company or props.get("company") or ""
    email = ev.email or props.get("email") or ""
    phone = format_phone(ev.phone or props.get("phone") or "")
    linkedin = usable_linkedin(ev.linkedin_url or props.get("hs_linkedin_url") or "")
    role = ev.title or props.get("jobtitle") or ""
    meeting_at = ev.extra.get("meeting_at")
    when = ""
    if isinstance(meeting_at, datetime):
        when = format_when(meeting_at)
    elif ev.extra.get("meeting_when"):
        when = ev.extra["meeting_when"]
    event_type = ev.extra.get("event_type") or "SalesGlider Intro"
    pain = (facts.get("pain_points") or props.get("pain_points") or ev.extra.get("pain") or "").strip()
    personal = (
        facts.get("personal_details")
        or props.get("personal_details")
        or ev.extra.get("personal")
        or ""
    ).strip()
    hooks = (facts.get("relationship_hooks") or props.get("relationship_hooks") or "").strip()
    offer = (facts.get("gift_ideas") or props.get("gift_ideas") or "").strip() or default_offer(company, role, pain)
    why = ev.extra.get("why") or _why_line(event_type, when, role, company, personal or pain or hooks)
    if not pain:
        pain = hooks or "See notes on the contact."
    if not personal:
        site = ev.domain or (props.get("website") or "").replace("https://", "").replace("http://", "").rstrip("/")
        personal = f"{role + ' at ' if role else ''}{company}.".strip()
        if site:
            personal = f"{personal} Site {site}."
    return "\n".join(
        [
            f"Brief: {name}",
            "",
            f"Company: {company or 'unknown'}",
            f"Email: {email or 'unknown'}",
            f"Phone: {phone or 'unknown'}",
            f"LinkedIn: {linkedin or 'unknown'}",
            f"Role: {role or 'unknown'}",
            "",
            "Why you are talking",
            why,
            "",
            "Pain / hooks",
            pain,
            "",
            "Personal details",
            personal,
            "",
            "Offer that fits",
            offer,
        ]
    )


def _why_line(event_type: str, when: str, role: str, company: str, extra: str) -> str:
    bits = [event_type.strip() if event_type else "SalesGlider Intro"]
    if when:
        bits.append(when)
    line = " ".join(bits) + "."
    line += " Zoom."
    who = ", ".join(p for p in (role, company) if p)
    if who:
        line += f" {who}."
    extra = extra.replace("\n", " ").strip()
    if extra and extra.lower() not in line.lower():
        first = extra.split(".")[0].strip()
        blob = f"{role} {company}".lower().replace("&", "and")
        if first and first.lower().replace("&", "and") not in blob:
            line += f" {first}."
    return line


def send(settings: Settings, gmail: Gmail, ev: Engagement, facts: dict | None = None, contact: dict | None = None) -> None:
    body = render(ev, facts, contact)
    subject = f"Brief: {ev.display_name() or ev.company or ev.email}"
    gmail.send(settings.josh_brief_email, subject, body)


def sent_query(ev: Engagement) -> str:
    name = ev.display_name() or ev.email or ""
    return f'in:sent subject:"Brief: {name}" newer_than:21d'


def matches_sent_brief(subject: str, body: str, ev: Engagement) -> bool:
    """True if this Sent message is already the brief for this meeting."""
    name = (ev.display_name() or "").strip()
    if name and f"brief: {name}".lower() not in f"{subject} {body}".lower():
        return False
    meeting_at = ev.extra.get("meeting_at")
    if isinstance(meeting_at, datetime):
        when = format_when(meeting_at)
        if when and when in f"{subject}\n{body}":
            return True
    if ev.email and ev.email.lower() in (body or "").lower():
        return True
    return bool(name and subject.lower().startswith("brief:"))


def already_sent(gmail: Gmail, ev: Engagement) -> bool:
    """Durable lock. Railway cron has no disk, and Supabase REST can 503."""
    try:
        stubs = gmail.search(sent_query(ev), max_results=10)
    except Exception:
        return True
    for stub in stubs:
        try:
            msg = gmail.get(stub["id"])
            headers = gmail.headers_map(msg)
            if matches_sent_brief(headers.get("subject", ""), gmail.body_text(msg), ev):
                return True
        except Exception:
            return True
    return False


def collect_upcoming(settings: Settings, gmail: Gmail) -> list[Engagement]:
    """Josh Calendly bookings still ahead. Used only to time the one brief."""
    out: list[Engagement] = []
    seen: set[str] = set()
    for stub in gmail.search(
        'newer_than:30d from:calendly.com ("SalesGlider" OR "New Event") -canceled -cancelled -"no-show"',
        max_results=40,
    ):
        msg = gmail.get(stub["id"])
        headers = gmail.headers_map(msg)
        subject = headers.get("subject", "")
        sender = headers.get("from", "")
        body = gmail.body_text(msg)
        if "calendly" not in f"{sender} {subject}".lower():
            continue
        if any(w in f"{subject} {body}".lower() for w in ("canceled", "cancelled", "no-show", "no show")):
            continue
        cal = parse_calendly(subject, body)
        if not is_josh_meeting(subject, cal.get("event_type", "")):
            continue
        meeting_at = parse_meeting_at(subject, body)
        if not meeting_at or meeting_at <= now_utc():
            continue
        email = (cal.get("email") or "").lower()
        key = brief_key(email, meeting_at)
        if not email or key in seen:
            continue
        seen.add(key)
        ev = Engagement(
            source="brief",
            external_id=key,
            occurred_at=meeting_at,
            email=email,
            first_name=cal.get("first_name") or "",
            last_name=cal.get("last_name") or "",
            name=cal.get("name") or "",
            company=cal.get("company") or "",
            domain=cal.get("domain") or "",
            raw_subject=subject,
            extra={
                "event_type": cal.get("event_type") or "SalesGlider Intro",
                "meeting_when": cal.get("when") or format_when(meeting_at),
                "meeting_at": meeting_at,
                "has_zoom": "zoom" in body.lower(),
            },
        )
        out.append(ev)
    return out


def send_due(
    settings: Settings,
    gmail: Gmail,
    hs: HubSpot | None,
    memory: Memory,
    report: CycleReport,
) -> None:
    try:
        meetings = collect_upcoming(settings, gmail)
    except Exception as exc:
        report.errors.append(f"brief scan: {exc}")
        return
    for ev in meetings:
        meeting_at = ev.extra.get("meeting_at")
        if not isinstance(meeting_at, datetime) or not due_to_send(meeting_at):
            continue
        if memory.already_processed("brief", ev.external_id) or already_sent(gmail, ev):
            report.skipped.append(f"brief already sent {ev.email}")
            continue
        contact = None
        if hs and ev.email:
            try:
                contact = hs.find_contact(email=ev.email)
            except Exception:
                contact = None
        if contact:
            props = contact.get("properties") or {}
            ev.company = ev.company or props.get("company") or ""
            ev.title = props.get("jobtitle") or ""
            ev.phone = ev.phone or props.get("phone") or ""
            ev.linkedin_url = ev.linkedin_url or props.get("hs_linkedin_url") or ""
        try:
            send(settings, gmail, ev, contact=contact)
            memory.mark_processed("brief", ev.external_id, {"email": ev.email, "when": meeting_at.isoformat()})
            report.briefs_sent.append(f"{ev.display_name() or ev.email} {format_when(meeting_at)}")
        except Exception as exc:
            report.errors.append(f"brief {ev.email}: {exc}")
