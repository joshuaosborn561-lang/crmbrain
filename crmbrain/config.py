from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

CDT = ZoneInfo("America/Chicago")

# People Josh talks to who are not prospects.
PERSONAL_PHONES = {
    "+15614278965",  # Sarah
    "15614278965",
    "+19733030001",  # Jeremy Ciotola
    "19733030001",
    "+19415927144",  # Dad
    "19415927144",
    "+15612255142",  # Cayden
    "15612255142",
    "+19734613447",  # Nonna
    "19734613447",
}
# Exact full-name matches plus first-token matches for the short set.
PERSONAL_NAMES = {
    "sarah",
    "sarah osborn",
    "jeremy",
    "jeremy ciotola",
    "diana burns",
    "diana",
    "cayden",
    "cayden osborn",
    "dad",
    "mom",
    "father",
    "nonna",
}
PERSONAL_FIRST_NAMES = {"sarah", "jeremy", "diana", "cayden", "dad", "mom", "father", "nonna"}
# Never write these people to HubSpot (contacts, notes, or deals).
SEEDED_NON_DEAL_NAMES = (
    "cynthia hernandez",
    "alex branning",
    "chorbie",
)
SEEDED_NON_DEAL_EMAILS: tuple[str, ...] = ()
PERSONAL_FAMILY_INTENTS = frozenset({"personal", "family"})
JOSH_EMAILS = {
    "joshua@salesglidergrowth.com",
    "joshuaosborn561@gmail.com",
    "joshua@salescloudedgroup.com",
    "joshua.osborn@insight.com",
}
JOSH_DOMAINS = {
    "salesglidergrowth.com",
    "salescloudedgroup.com",
    "jmosolutionsllc.com",
    "insight.com",
}

POSITIVE_SMARTLEAD_CATEGORIES = {1, 2, 5}  # Interested, Meeting Request, Info Request
POSITIVE_SENTIMENTS = {"positive"}

STAGE = {
    "replied": "appointmentscheduled",
    "discovery_scheduled": "qualifiedtobuy",
    "discovery_completed": "presentationscheduled",
    "proposal_sent": "decisionmakerboughtin",
    "signed": "closedwon",
    "paid": "3482933986",
    "nurture": "3486952153",
    "no_show": "3557889773",
    "closed_lost": "closedlost",
}

INTERNAL_MEETING_HINTS = (
    "weekly",
    "day trading",
    "daytrade",
    "internal",
    "1:1 cayden",
    "cayden / josh",
    "josh / cayden",
)

# Meetings where Josh is the buyer, learner, networker, or not selling SG.
NON_SALES_TITLE_HINTS = (
    "marketing masterclass",
    "masterclass",
    "chorbie",
    "meraki",
    "dotstech",
    "dots tech",
    "cisco",
    "insight.com",
    "lunch",
    "coffee chat",
    "catch up",
    "recruiter",
    "recruiting",
    "mark/josh",
    "mark / josh",
    "josh/mark",
    "mentor",
    "seo partner",
)

# Josh's clients. Talk to them, but do not open a new SalesGlider deal.
CLIENT_HINTS = (
    "goliath",
    "vasco",
    "peterson",
    "roofs by peterson",
    "bolder cyber",
    "parlay",
    "culture fits",
    "tech evolution",
    "techevo",
    "msrs",
    "kyle peterson",
    "corey tapper",
    "dave ackley",
    "carlos vasquez",
    "randy haba",
    "tj johnson",
)


@dataclass(frozen=True)
class Settings:
    hubspot_token: str
    gmail_client_id: str
    gmail_client_secret: str
    gmail_refresh_token: str
    josh_brief_email: str
    fireflies_key: str
    smartlead_key: str
    cube_folder: str
    heyreach_url: str
    heyreach_key: str
    heyreach_campaign_id: int
    heyreach_linkedin_account_id: int
    enrichment_url: str
    enrichment_client_tag: str
    leadmagic_key: str
    slack_token: str
    slack_channel: str
    supabase_url: str
    supabase_key: str
    gemini_key: str
    gemini_model: str
    allo_url: str
    allo_key: str
    lookback_hours: int
    lookback_start_at: datetime | None = None
    lookback_override: bool = False
    dry_run: bool = False
    intent_min_confidence: float = 0.75
    calendar_upcoming_days: int = 30
    max_archives_regressions: int = 10
    max_creates: int = 10
    max_stage_moves: int = 20
    max_amount_writes: int = 20
    max_change_fraction: float = 0.15
    reextract_since: datetime | None = None
    google_api_key: str = ""
    cube_lookback_days: int = 14

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            hubspot_token=os.getenv("HUBSPOT_ACCESS_TOKEN", ""),
            gmail_client_id=os.getenv("GMAIL_CLIENT_ID", ""),
            gmail_client_secret=os.getenv("GMAIL_CLIENT_SECRET", ""),
            gmail_refresh_token=os.getenv("GMAIL_REFRESH_TOKEN", ""),
            josh_brief_email=os.getenv("JOSH_BRIEF_EMAIL", "joshua@salesglidergrowth.com"),
            fireflies_key=os.getenv("FIREFLIES_API_KEY", ""),
            smartlead_key=os.getenv("SMARTLEAD_API_KEY", ""),
            cube_folder=os.getenv("CUBE_ACR_DRIVE_FOLDER", "1buFUvvaRUhfnu995tfI0s7FDBWsRFAnp"),
            heyreach_url=os.getenv("HEYREACH_MCP_URL", "https://mcp.heyreach.io/mcp"),
            heyreach_key=os.getenv("HEYREACH_MCP_KEY", ""),
            heyreach_campaign_id=int(os.getenv("HEYREACH_CAMPAIGN_ID", "530529")),
            heyreach_linkedin_account_id=int(os.getenv("HEYREACH_LINKEDIN_ACCOUNT_ID", "154688")),
            enrichment_url=os.getenv(
                "ENRICHMENT_MCP_URL",
                "https://email-waterfall-production-021b.up.railway.app/mcp",
            ),
            enrichment_client_tag=os.getenv("ENRICHMENT_CLIENT_TAG", "salesglider"),
            leadmagic_key=os.getenv("LEADMAGIC_API_KEY", ""),
            slack_token=os.getenv("SLACK_BOT_TOKEN", ""),
            slack_channel=os.getenv("SLACK_NURTURE_CHANNEL", "C0BHBDTMRFY"),
            supabase_url=os.getenv("SUPABASE_URL", "https://azpapwtnrbzywlnxxecz.supabase.co"),
            supabase_key=os.getenv("SUPABASE_SERVICE_ROLE_KEY", ""),
            gemini_key=os.getenv("GEMINI_API_KEY", ""),
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
            allo_url=os.getenv("ALLO_API_URL", "https://api.withallo.com"),
            allo_key=os.getenv("ALLO_API_KEY", ""),
            lookback_hours=int(os.getenv("CYCLE_LOOKBACK_HOURS", "36")),
            lookback_start_at=_parse_lookback_start(os.getenv("CRMBRAIN_LOOKBACK_START", "")),
            lookback_override=bool(os.getenv("CRMBRAIN_LOOKBACK_START", "").strip()),
            dry_run=os.getenv("CRMBRAIN_DRY_RUN", "").strip().lower() in {"1", "true", "yes"},
            intent_min_confidence=float(os.getenv("INTENT_MIN_CONFIDENCE", "0.75")),
            calendar_upcoming_days=int(os.getenv("CALENDAR_UPCOMING_DAYS", "30")),
            max_archives_regressions=int(os.getenv("MAX_ARCHIVES_REGRESSIONS", "10")),
            max_creates=int(os.getenv("MAX_CREATES", "10")),
            max_stage_moves=int(os.getenv("MAX_STAGE_MOVES", "20")),
            max_amount_writes=int(os.getenv("MAX_AMOUNT_WRITES", "20")),
            max_change_fraction=float(os.getenv("MAX_CHANGE_FRACTION", "0.15")),
            reextract_since=_parse_lookback_start(os.getenv("CRMBRAIN_REEXTRACT_SINCE", "")),
            google_api_key=os.getenv("GOOGLE_API_KEY", ""),
            cube_lookback_days=int(os.getenv("CUBE_LOOKBACK_DAYS", "14")),
        )


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_cdt() -> datetime:
    return datetime.now(CDT)


LOOKBACK_OVERLAP_HOURS = 2
LOOKBACK_CAP_HOURS = 24 * 7


def lookback_start(hours: int, start_at: datetime | None = None) -> datetime:
    if start_at is not None:
        return start_at if start_at.tzinfo else start_at.replace(tzinfo=timezone.utc)
    return now_utc() - timedelta(hours=hours)


def settings_lookback_start(settings: Settings) -> datetime:
    return lookback_start(settings.lookback_hours, settings.lookback_start_at)


def _parse_lookback_start(raw: str) -> datetime | None:
    """CRMBRAIN_LOOKBACK_START: ISO date (Chicago midnight) or ISO datetime."""
    text = (raw or "").strip()
    if not text:
        return None
    try:
        if len(text) == 10 and text[4] == "-" and text[7] == "-":
            day = date.fromisoformat(text)
            return datetime(day.year, day.month, day.day, tzinfo=CDT)
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def compute_lookback_start(
    settings: Settings,
    last_started_at: datetime | None,
    now: datetime | None = None,
    overlap_hours: int = LOOKBACK_OVERLAP_HOURS,
    cap_hours: int = LOOKBACK_CAP_HOURS,
) -> datetime:
    """Window start: last finished cycle minus overlap, else the configured hours.

    Monday 7am after a Friday 5pm run must include Friday evening. Cap at 7 days
    so a long outage does not replay the whole history.

    CRMBRAIN_LOOKBACK_START (lookback_override) wins and is not capped.
    """
    if getattr(settings, "lookback_override", False) and settings.lookback_start_at:
        start = settings.lookback_start_at
        return start if start.tzinfo else start.replace(tzinfo=timezone.utc)
    now = now or now_utc()
    fallback = now - timedelta(hours=settings.lookback_hours)
    if last_started_at is None:
        return fallback
    if last_started_at.tzinfo is None:
        last_started_at = last_started_at.replace(tzinfo=timezone.utc)
    start = last_started_at - timedelta(hours=overlap_hours)
    earliest = now - timedelta(hours=cap_hours)
    if start < earliest:
        return earliest
    return start


def lookback_dates_cdt(settings: Settings) -> list[str]:
    """Inclusive Chicago dates from the cycle window through today."""
    start = settings_lookback_start(settings).astimezone(CDT).date()
    end = now_cdt().date()
    if start > end:
        return [end.isoformat()]
    days = (end - start).days
    return [(end - timedelta(days=i)).isoformat() for i in range(days, -1, -1)]


def gmail_after_clause(start: datetime) -> str:
    local = start.astimezone(CDT)
    return f"after:{local.year}/{local.month:02d}/{local.day:02d}"


def today_and_yesterday_cdt() -> list[str]:
    today = now_cdt().date()
    return [(today - timedelta(days=i)).isoformat() for i in (0, 1)]


def digits_phone(value: str | None) -> str:
    if not value:
        return ""
    return "".join(ch for ch in value if ch.isdigit())


def _csv_env(name: str) -> set[str]:
    return {part.strip() for part in os.getenv(name, "").split(",") if part.strip()}


def personal_numbers() -> set[str]:
    """Seeded family numbers plus PERSONAL_NUMBERS (comma-separated)."""
    phones = set(PERSONAL_PHONES)
    for raw in _csv_env("PERSONAL_NUMBERS"):
        phones.add(raw)
        digits = digits_phone(raw)
        if digits:
            phones.add(digits)
            phones.add(f"+{digits}")
    return phones


def non_deal_emails() -> set[str]:
    emails = {e.strip().lower() for e in SEEDED_NON_DEAL_EMAILS if e.strip()}
    emails.update(e.lower() for e in _csv_env("NON_DEAL_EMAILS"))
    return emails


def non_deal_names() -> set[str]:
    names = {n.strip().lower() for n in SEEDED_NON_DEAL_NAMES if n.strip()}
    names.update(n.lower() for n in _csv_env("NON_DEAL_NAMES"))
    return names


ZOOM_ROOM_DOMAINS = frozenset({"zoomcrc.com", "zoom.com", "zoomgov.com"})


def is_zoom_room_address(email: str | None) -> bool:
    """Zoom room / CRC addresses are rooms, not people."""
    low = (email or "").strip().lower()
    if not low or "@" not in low:
        return False
    local, domain = low.rsplit("@", 1)
    if domain in ZOOM_ROOM_DOMAINS or domain.endswith(".zoomcrc.com"):
        return True
    if "zoom" in domain and (local.isdigit() or local.startswith("room")):
        return True
    return False


def is_josh_address(email: str | None) -> bool:
    """Josh's own mailboxes — JOSH_EMAILS plus every address on JOSH_DOMAINS."""
    low = (email or "").strip().lower()
    if not low:
        return False
    if low in JOSH_EMAILS:
        return True
    if "@" not in low:
        return False
    return low.rsplit("@", 1)[-1] in JOSH_DOMAINS


def is_personal(name: str | None = None, phone: str | None = None, email: str | None = None) -> bool:
    if email and is_josh_address(email):
        return False
    if phone:
        raw = digits_phone(phone)
        phones = personal_numbers()
        if raw in phones or f"+{raw}" in phones:
            return True
        if raw[-10:] in {p[-10:] for p in phones if len(p) >= 10}:
            return True
    if name:
        n = name.strip().lower()
        if n in PERSONAL_NAMES:
            return True
        for token in PERSONAL_NAMES:
            if " " in token and token in n:
                return True
        first = n.split()[0] if n else ""
        if first in PERSONAL_FIRST_NAMES:
            return True
    return False


def date_window_cdt(days: int) -> list[str]:
    """Inclusive Chicago dates covering the last `days` through today."""
    end = now_cdt().date()
    span = max(1, int(days or 1))
    return [(end - timedelta(days=i)).isoformat() for i in range(span, -1, -1)]


def is_excluded_contact(ev=None, contact: dict | None = None) -> bool:
    """True when the engagement or HubSpot contact is on the non-deal list."""
    props = (contact or {}).get("properties") or {}
    name = ""
    email = ""
    company = ""
    phone = ""
    if ev is not None:
        display = getattr(ev, "display_name", None)
        name = (display() if callable(display) else "") or getattr(ev, "name", "") or ""
        email = getattr(ev, "email", "") or ""
        company = getattr(ev, "company", "") or ""
        phone = getattr(ev, "phone", "") or ""
    name = name or f"{props.get('firstname') or ''} {props.get('lastname') or ''}".strip()
    email = email or props.get("email") or ""
    company = company or props.get("company") or ""
    phone = phone or props.get("phone") or ""
    return is_non_deal_person(name=name, email=email, company=company, phone=phone)


def is_non_deal_person(
    name: str | None = None,
    email: str | None = None,
    company: str | None = None,
    phone: str | None = None,
) -> bool:
    """Hard block: no HubSpot contact, note, or deal writes for these people."""
    email_l = (email or "").strip().lower()
    if email_l and email_l in non_deal_emails():
        return True
    blob = " ".join(part for part in (name or "", company or "", email_l, phone or "") if part).lower()
    if not blob.strip():
        return False
    for token in non_deal_names():
        if token and token in blob:
            return True
    return False


def is_personal_family_intent(intent: str | None) -> bool:
    value = (intent or "").strip().lower()
    if not value:
        return False
    if value in PERSONAL_FAMILY_INTENTS:
        return True
    return "family" in value


def is_client_context(name: str = "", company: str = "", title: str = "") -> bool:
    blob = f"{name} {company} {title}".lower()
    return any(h in blob for h in CLIENT_HINTS)


def is_internal_meeting(title: str | None, participants: list[str] | None = None) -> bool:
    t = (title or "").lower()
    if any(h in t for h in INTERNAL_MEETING_HINTS):
        return True
    emails = [p.lower() for p in (participants or []) if "@" in p]
    if emails and all(e in JOSH_EMAILS or "cayden" in e for e in emails):
        return True
    return False
