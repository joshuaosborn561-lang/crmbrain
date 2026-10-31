"""SalesGlider lead source (sg_lead_source) stamped on HubSpot contacts and deals at booking time.

Every option is detected from data crmbrain already sees. Confirmed = a dated record names the channel.
Inferred = circumstantial. Nothing matched = left blank. An existing value is never overwritten.

Signal order (first hit wins):
 1. The engagement itself is a channel record
      smartlead reply   -> campaign name decides (see CAMPAIGN_RULES)          confirmed
      heyreach          -> LinkedIn/HeyReach                                   confirmed
      allo / rvm        -> Cold call                                           confirmed
 2. Referral from Gmail: the earliest thread with the prospect has a third party on it
      known referral account (REFERRAL_ACCOUNTS) or intro wording             confirmed
      cc'd third party with no intro wording                                   inferred
 3. Cold call: an Allo call to the prospect's phone in the 21 days before the booking   confirmed
      (same name + company only, no phone match)                                         inferred
 4. LinkedIn: a HeyReach row in crmbrain.ticker for the email                    confirmed
 5. Smartlead lead lookup (every campaign the email sits in); the campaign name decides
      replied in that campaign, or the booking row carries a Smartlead send_id   confirmed
      only sent to                                                               inferred
 6. Weak history: ticker rows (smartlead/heyreach/rvm) or the contact's lead_source_platofrm   inferred
 7. Inbound/other, only when 2-6 found no outbound touch
      HubSpot form conversion, or a booking whose referrer is our own site       confirmed
      direct Calendly booking / web-traffic analytics source                     inferred
      (inferred is skipped while Allo calls are unreadable: a cold-call booking looks like a direct one)
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

SOURCE_PROP = "sg_lead_source"
CONFIDENCE_PROP = "sg_lead_source_confidence"
CONTACT_EVIDENCE_PROPS = (
    "phone",
    "lead_source_platofrm",
    "first_conversion_event_name",
    "hs_analytics_source",
)

REFERRAL = "Referral"
COLD_EMAIL = "Cold email"
LINKEDIN = "LinkedIn/HeyReach"
COLD_CALL = "Cold call"
LIVE_LEADS = "Live leads"
PARTNER_PLAY = "Partner play"
INBOUND_OTHER = "Inbound/other"
LEAD_SOURCES = (REFERRAL, COLD_EMAIL, LINKEDIN, COLD_CALL, LIVE_LEADS, PARTNER_PLAY, INBOUND_OTHER)
CONFIRMED = "confirmed"
INFERRED = "inferred"

# Smartlead campaign name -> source. First match wins. Names are as seen in the Smartlead API
# (e.g. "LiveLeads | Staffing Owners | 2026-10", "SG Influencer Partnerships 1007",
# "SG Referral Hosts 1015 - IT", "Cold Call Followup", "SalesGlider Warm Web Visitors").
CAMPAIGN_RULES: tuple[tuple[re.Pattern, str, str | None], ...] = (
    (re.compile(r"live\s*-?leads", re.I), LIVE_LEADS, None),
    (re.compile(r"influencer|partnerships?\b|partner\s+(play|program)", re.I), PARTNER_PLAY, None),
    (re.compile(r"referral\s+hosts?|referral\s+partners?", re.I), REFERRAL, None),
    (re.compile(r"cold\s+call", re.I), COLD_CALL, None),
    # Web-visitor lists are people who found us first. Weak, so capped at inferred.
    (re.compile(r"web\s+visitors?|rb2b|web\s+intent", re.I), INBOUND_OTHER, INFERRED),
)

# Accounts whose intros mean "referral": people and clients who send us business.
DEFAULT_REFERRAL_ACCOUNTS = (
    "culture-fits.com",  # TJ
    "parlaytech.net",  # Randy Haba, Parlay
    "bpltg.com",  # Christian Batten, BPL
    "boldercyberpartners.com",  # Mike Trpkosh, Bolder
)
_INTRO_RE = re.compile(
    r"\bintro\b|\bintros\b|introduc|in touch with|connect(?:ing)? (?:you|the two)|\breferr(?:al|ing|ed)\b|sales resource",
    re.I,
)
ALLO_LOOKBACK_DAYS = 21
WEB_ANALYTICS_SOURCES = frozenset(
    {"ORGANIC_SEARCH", "DIRECT_TRAFFIC", "PAID_SEARCH", "PAID_SOCIAL", "SOCIAL_MEDIA", "REFERRALS"}
)
SITE_REFERRER_HINTS = ("salesglidergrowth.com",)

_DIRECT = {"heyreach": LINKEDIN, "allo": COLD_CALL, "rvm": COLD_CALL}
_HISTORY = {"smartlead": COLD_EMAIL, "heyreach": LINKEDIN, "rvm": COLD_CALL, "allo": COLD_CALL}


@dataclass
class LeadSourceContext:
    """Everything the resolver may read. Any piece may be None; each lookup fails soft."""

    settings: Any = None
    memory: Any = None
    gmail: Any = None
    booking_at: datetime | None = None
    errors: list[str] = field(default_factory=list)  # lookups that failed: "no outbound touch" is unproven
    gaps: list[str] = field(default_factory=list)  # sources we know we cannot read (e.g. allo schema not exposed)

    def gap(self, msg: str) -> None:
        if msg not in self.gaps:
            logger.info("lead_source gap: %s", msg)
            self.gaps.append(msg)

    def note(self, msg: str) -> None:
        logger.warning("lead_source: %s", msg)
        self.errors.append(msg)


def referral_accounts() -> set[str]:
    raw = os.getenv("REFERRAL_ACCOUNTS", "")
    extra = {t.strip().lower() for t in raw.split(",") if t.strip()}
    return {a.lower() for a in DEFAULT_REFERRAL_ACCOUNTS} | extra


def _norm_email(email: str | None) -> str:
    return (email or "").strip().lower()


def _digits10(phone: str | None) -> str:
    d = "".join(c for c in (phone or "") if c.isdigit())
    return d[-10:] if len(d) >= 10 else ""


def classify_campaign(name: str | None) -> tuple[str, str | None]:
    """(source, confidence_cap). Anything not matched is plain Cold email."""
    for pattern, source, cap in CAMPAIGN_RULES:
        if pattern.search(name or ""):
            return source, cap
    return COLD_EMAIL, None


def _cap(confidence: str, cap: str | None) -> str:
    return INFERRED if cap == INFERRED else confidence


def _rows(ctx: LeadSourceContext | None, schema: str, table: str, params: dict, *, soft: bool = False) -> list[dict]:
    memory = getattr(ctx, "memory", None)
    if memory is None or not getattr(memory, "use_supabase", False):
        return []
    try:
        return list(memory._sb_named(schema, "GET", table, params=params) or [])
    except Exception as exc:  # evidence lookups must never break the cycle
        if ctx is not None:
            (ctx.gap if soft else ctx.note)(f"{schema}.{table}: {str(exc)[:120]}")
        return []


# ---- individual signals -------------------------------------------------------------------------


def from_engagement(ev: Any) -> tuple[str, str] | None:
    source = (getattr(ev, "source", "") or "").lower()
    if source in _DIRECT:
        return _DIRECT[source], CONFIRMED
    if source == "smartlead":
        extra = getattr(ev, "extra", None) or {}
        mapped, cap = classify_campaign(extra.get("campaign_name"))
        if extra.get("client_campaign") and mapped == COLD_EMAIL:
            return COLD_EMAIL, INFERRED  # lead in a client's campaign, then booked with us
        return mapped, _cap(CONFIRMED, cap)
    return None


def _third_parties(headers: dict[str, str], prospect: str) -> list[str]:
    from crmbrain.config import is_josh_address
    from crmbrain.sources.gmail_scan import _addresses, is_system_address

    out: list[str] = []
    for key in ("from", "to", "cc"):
        for addr in _addresses(headers.get(key, "")):
            low = _norm_email(addr)
            if (
                not low
                or low == prospect
                or is_josh_address(low)
                or "cayden" in low
                or is_system_address(low)
                or low in out
            ):
                continue
            out.append(low)
    return out


def _names_prospect(subject: str, ev: Any) -> bool:
    low = (subject or "").lower()
    first = (getattr(ev, "first_name", "") or "").strip().lower()
    last = (getattr(ev, "last_name", "") or "").strip().lower()
    company = (getattr(ev, "company", "") or "").strip().lower()
    return any(tok and len(tok) > 2 and tok in low for tok in (first, last, company))


def _is_known(addrs: list[str]) -> bool:
    known = referral_accounts()
    return any(a in known or a.rsplit("@", 1)[-1] in known for a in addrs)


def _oldest(gmail: Any, query: str, keep: int) -> list[dict]:
    """Gmail search is newest-first, so the tail of one big page holds the oldest messages."""
    stubs = (gmail.search(query, max_results=100) or [])[-keep:]
    msgs = [gmail.get(str(s["id"])) for s in stubs if (s or {}).get("id")]
    msgs.sort(key=lambda m: int(m.get("internalDate") or 0))
    return msgs


def _classify_intro_message(
    gmail: Any, msg: dict, email: str, ev: Any, *, third_party_sender: bool = False
) -> tuple[str, str] | None:
    """One message that names/addresses the prospect. With third_party_sender, the prospect (or Josh)
    sending it never counts: an intro about someone comes from somebody else."""
    from crmbrain.sources.gmail_scan import _addresses

    headers = gmail.headers_map(msg)
    senders = _third_parties({"from": headers.get("from", "")}, email)
    third = _third_parties(headers, email)
    if not third or (third_party_sender and not senders):
        return None
    subject = headers.get("subject", "")
    blob = f"{subject}\n{gmail.body_text(msg)[:1500]}"
    intro = bool(_INTRO_RE.search(blob))
    if _is_known(third) and (intro or senders):
        return REFERRAL, CONFIRMED  # a known referral account is on this thread with them
    in_main = email in _addresses(headers.get("from", "")) or email in _addresses(headers.get("to", ""))
    if not in_main:
        # prospect only cc'd or merely mentioned: they may be the introducer, so no signal
        return None
    if intro:
        return REFERRAL, (CONFIRMED if _names_prospect(subject, ev) else INFERRED)
    return REFERRAL, INFERRED  # third party on the thread with them, no intro wording


def referral_from_gmail(ctx: LeadSourceContext | None, email: str, ev: Any = None) -> tuple[str, str] | None:
    """Referral signals, strongest wins:
    - a known referral account (or intro wording naming the prospect) on the earliest thread with them
    - an intro message from a third party that names the prospect, even if the prospect is not on the
      thread (a forwarded "intro" to Josh)
    - a third party cc'd on the earliest thread, no intro wording (inferred)
    """
    gmail = getattr(ctx, "gmail", None)
    if gmail is None or not email:
        return None
    best: tuple[str, str] | None = None
    try:
        earliest = _oldest(gmail, f"(from:{email} OR to:{email} OR cc:{email}) -in:chats", keep=6)
        if earliest:
            best = _classify_intro_message(gmail, earliest[0], email, ev)
        if best and best[1] == CONFIRMED:
            return best
        first = (getattr(ev, "first_name", "") or "").strip()
        last = (getattr(ev, "last_name", "") or "").strip()
        who = f'"{first} {last}" OR ' if first and last else ""
        words = '(intro OR introduction OR introducing OR introduce OR referral OR "sales resource")'
        for msg in _oldest(gmail, f'({who}"{email}") {words} -from:me -in:chats', keep=4):
            hit = _classify_intro_message(gmail, msg, email, ev, third_party_sender=True)
            if hit and hit[1] == CONFIRMED:
                return hit
            if hit and not best:
                best = hit
        return best
    except Exception as exc:
        ctx.note(f"gmail referral lookup: {exc}")
        return None


# PostgREST only serves the `allo` schema when it is in Supabase's exposed-schemas list. If it is not,
# a view named public.allo_calls_v (same columns) is read instead. Neither present = a known gap.
ALLO_SOURCES = (("allo", "calls"), ("public", "allo_calls_v"))


def _allo_rows(ctx: LeadSourceContext | None, params: dict) -> list[dict]:
    memory = getattr(ctx, "memory", None)
    if memory is None or not getattr(memory, "use_supabase", False):
        return []
    last = ""
    for schema, table in ALLO_SOURCES:
        try:
            return list(memory._sb_named(schema, "GET", table, params=params) or [])
        except Exception as exc:
            last = f"{schema}.{table}: {str(exc)[:100]}"
    ctx.gap(f"allo calls unreadable ({last})")
    return []


def cold_call_from_allo(ctx: LeadSourceContext | None, ev: Any, contact: dict | None) -> tuple[str, str] | None:
    props = (contact or {}).get("properties") or {}
    last10 = _digits10(getattr(ev, "phone", "") or props.get("phone"))
    booking = getattr(ctx, "booking_at", None) or datetime.now(timezone.utc)
    since = (booking - timedelta(days=ALLO_LOOKBACK_DAYS)).isoformat()
    until = (booking + timedelta(days=1)).isoformat()
    window = {"call_at": f"gte.{since}", "and": f"(call_at.lte.{until})", "limit": "5"}
    if last10:
        if _allo_rows(ctx, {"select": "id", "direction": "eq.OUTBOUND", "contact_number": f"ilike.*{last10}", **window}):
            return COLD_CALL, CONFIRMED
    name = (getattr(ev, "display_name", lambda: "")() or "").strip()
    if name and " " in name:
        if _allo_rows(ctx, {"select": "id", "direction": "eq.OUTBOUND", "contact_name": f"ilike.{name}", **window}):
            return COLD_CALL, INFERRED
    return None


def _booking_rows(ctx: LeadSourceContext | None, email: str) -> list[dict]:
    return _rows(
        ctx,
        "public",
        "booking_events",
        {
            "select": "send_id,referrer,campaign_uuid,platform",
            "lead_email": f"ilike.{email}",
            "event_type": "eq.booking_confirmed",
            "order": "created_at.desc",
            "limit": "3",
        },
    )


def from_smartlead_lookup(
    ctx: LeadSourceContext | None, email: str, bookings: list[dict]
) -> tuple[str, str] | None:
    settings = getattr(ctx, "settings", None)
    has_send_id = any(b.get("send_id") for b in bookings)
    campaigns: list[dict] = []
    if settings is not None and getattr(settings, "smartlead_key", ""):
        try:
            from crmbrain.sources import smartlead

            campaigns = smartlead.lead_campaigns(settings, email)
        except Exception as exc:
            ctx.note(f"smartlead lead lookup: {exc}")
    if campaigns:
        replied = [c for c in campaigns if c.get("last_reply_at")]
        pool = replied or campaigns
        chosen = max(pool, key=lambda c: str(c.get("last_reply_at") or c.get("last_sent_at") or ""))
        source, cap = classify_campaign(chosen.get("campaign_name"))
        strong = bool(replied) or has_send_id
        return source, _cap(CONFIRMED if strong else INFERRED, cap)
    if has_send_id:
        return COLD_EMAIL, CONFIRMED  # booked from a Smartlead link; campaign name unavailable
    return None


def from_history(ctx: LeadSourceContext | None, email: str, contact: dict | None) -> tuple[str, str] | None:
    rows = _rows(ctx, "crmbrain", "ticker", {"select": "source", "email": f"ilike.{email}", "limit": "20"})
    mapped = {_HISTORY.get((r.get("source") or "").strip().lower()) for r in rows} - {None}
    if LINKEDIN in mapped:
        return LINKEDIN, CONFIRMED  # a dated HeyReach conversation row names the channel
    for pick in (COLD_CALL, COLD_EMAIL):
        if pick in mapped:
            return pick, INFERRED
    platform = (((contact or {}).get("properties") or {}).get("lead_source_platofrm") or "").strip().lower()
    hit = _HISTORY.get(platform)
    return (hit, INFERRED) if hit else None


def inbound_signal(
    ev: Any, contact: dict | None, bookings: list[dict], *, confirmed_only: bool = False
) -> tuple[str, str] | None:
    props = (contact or {}).get("properties") or {}
    if (props.get("first_conversion_event_name") or "").strip():
        return INBOUND_OTHER, CONFIRMED  # submitted a site form
    for b in bookings:
        ref = (b.get("referrer") or "").lower()
        if ref and any(h in ref for h in SITE_REFERRER_HINTS):
            return INBOUND_OTHER, CONFIRMED
    if confirmed_only:
        return None
    if (props.get("hs_analytics_source") or "").upper() in WEB_ANALYTICS_SOURCES:
        return INBOUND_OTHER, INFERRED
    source = (getattr(ev, "source", "") or "").lower()
    extra = getattr(ev, "extra", None) or {}
    if bookings or source == "calendly" or extra.get("create_new"):
        return INBOUND_OTHER, INFERRED  # booked directly, no outbound touch found
    return None


# ---- resolver -----------------------------------------------------------------------------------


def resolve_lead_source(
    ev: Any, ctx: LeadSourceContext | None = None, contact: dict | None = None
) -> tuple[str, str] | None:
    """Return (source, confidence) or None when there is truly nothing."""
    if ctx is not None:
        ctx = replace(ctx, errors=[])  # per-booking error tracking; lookups stay shared
    hit = from_engagement(ev)
    if hit:
        return hit
    email = _norm_email(getattr(ev, "email", "")) or _norm_email(
        (((contact or {}).get("properties")) or {}).get("email")
    )
    if email:
        hit = referral_from_gmail(ctx, email, ev)
        if hit and hit[1] == CONFIRMED:
            return hit
        weak_referral = hit
    else:
        weak_referral = None
    hit = cold_call_from_allo(ctx, ev, contact)
    if hit and hit[1] == CONFIRMED:
        return hit
    weak_call = hit
    bookings = _booking_rows(ctx, email) if email else []
    hit = from_history(ctx, email, contact) if email else None
    if hit and hit[0] == LINKEDIN and hit[1] == CONFIRMED:
        return hit
    history = hit
    sl = from_smartlead_lookup(ctx, email, bookings) if email else None
    if sl and sl[1] == CONFIRMED:
        return sl
    for weak in (weak_referral, weak_call, sl, history):
        if weak:
            return weak
    if ctx is not None and ctx.errors:
        return None  # a lookup failed, so "no outbound touch" is not established
    # Allo unreadable: a cold-call booking looks exactly like a direct booking, so only confirmed inbound.
    allo_blind = bool(ctx is not None and any("allo" in g for g in ctx.gaps))
    return inbound_signal(ev, contact, bookings, confirmed_only=allo_blind)
