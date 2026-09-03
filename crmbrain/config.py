from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
}
PERSONAL_FIRST_NAMES = {"sarah", "jeremy", "diana", "cayden", "dad", "mom", "father"}
JOSH_EMAILS = {
    "joshua@salesglidergrowth.com",
    "joshuaosborn561@gmail.com",
    "joshua@salescloudedgroup.com",
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
            allo_url=os.getenv("ALLO_API_URL", ""),
            allo_key=os.getenv("ALLO_API_KEY", ""),
            lookback_hours=int(os.getenv("CYCLE_LOOKBACK_HOURS", "36")),
        )


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_cdt() -> datetime:
    return datetime.now(CDT)


def lookback_start(hours: int) -> datetime:
    return now_utc() - timedelta(hours=hours)


def today_and_yesterday_cdt() -> list[str]:
    today = now_cdt().date()
    return [(today - timedelta(days=i)).isoformat() for i in (0, 1)]


def digits_phone(value: str | None) -> str:
    if not value:
        return ""
    return "".join(ch for ch in value if ch.isdigit())


def is_personal(name: str | None = None, phone: str | None = None, email: str | None = None) -> bool:
    if email and email.lower() in JOSH_EMAILS:
        return False
    if phone:
        raw = digits_phone(phone)
        if raw in PERSONAL_PHONES or f"+{raw}" in PERSONAL_PHONES:
            return True
        if raw[-10:] in {p[-10:] for p in PERSONAL_PHONES if len(p) >= 10}:
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
