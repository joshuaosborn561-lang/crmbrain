"""#nurture ticker rebuild: enrollment, drafts, Slack cards, Gmail thread send."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from crmbrain.config import (
    CDT,
    RENEWAL_PIPELINE,
    STAGE,
    Settings,
    canonicalize_stage,
    is_archived_hs_row,
    is_client_context,
    is_josh_address,
    is_non_deal_person,
    now_utc,
)
from crmbrain.memory import Memory
from crmbrain.models import CycleReport, Engagement
from crmbrain.policy import deal_is_locked, event_predates_freeze, is_meeting_held, is_meeting_scheduled
from crmbrain.ticker import (
    HARD_STOPS,
    MEETING_GUARANTEE,
    SOFT_STOPS,
    TICKER_DAYS,
    TickerCandidate,
    VERTICALS,
    _no_dashes,
    already_enrolled,
    has_free_poc_offer,
    parse_signal_at,
)

logger = logging.getLogger(__name__)

# Josh: no AirPods or tickets in nurture copy.
AIRPODS_OFFER_LIVE = False
NURTURE_MAX_PER_WEEKDAY = 5
EMAILED_RECENTLY_DAYS = 60
STALLED_DAYS = 30
SNIPPET_MAX = 280
MAX_BODY_WORDS = 110
JOSH_FROM = "joshua@salesglidergrowth.com"
SMARTLEAD_CLIENT_ID = 345263
POSITIVE_CATEGORY_IDS = frozenset({1, 2, 5, 131482})
MEETING_ENROLL_SOURCES = frozenset({"fireflies", "calendly", "cube_acr", "allo"})
REPLY_ONLY_SOURCES = frozenset({"smartlead", "heyreach", "rvm", "gmail_person", "gmail"})

GENERAL_PROOF = (
    "$2M in pipeline last quarter, one client closed $100K in their first 3 months, "
    "averaging 14+ replies per month."
)
TRADES_PROOF = (
    "$2M in pipeline last quarter across our trades clients, "
    "one closed $100K in their first 3 months."
)
ROOFING_PROOF = "one of our roofers closed $100K in his first 3 months with us."
CASE_STUDIES = {
    "roofing": ROOFING_PROOF,
    "hvac": TRADES_PROOF,
    "construction": TRADES_PROOF,
    "plumbing": TRADES_PROOF,
    "electrical": TRADES_PROOF,
    "solar": TRADES_PROOF,
    "trades": TRADES_PROOF,
    "home_services": TRADES_PROOF,
}
APPROVED_PROOF_LINES = frozenset({GENERAL_PROOF, TRADES_PROOF, ROOFING_PROOF})
PROOF_ALIASES = {
    "recruiting": "staffing",
    "it": "msp",
    "cyber": "msp",
    "trades": "construction",
    "home_services": "construction",
}
NURTURE_EXCLUDE_NAMES = frozenset({"kevin hagemoser"})
NURTURE_EXCLUDE_EMAILS = frozenset({"kevin@kevinhagemoser.com"})
HELD_MEETING_SOURCES = frozenset({"fireflies", "cube_acr", "cube", "allo"})
HELD_NEAR_DAYS = 2
WANT_CONFIDENCE_MIN = 0.7
TRUSTED_MEETING_DATE_SOURCES = frozenset(
    {
        "fireflies",
        "cube",
        "cube_acr",
        "allo",
        "calendly",
        "gmail_booking",
        "gmail_followup",
        "hs_meeting",
    }
)
_FORBIDDEN_DATE_KEYS = (
    "createdate",
    "hs_createdate",
    "hs_lastmodifieddate",
    "lastmodifieddate",
    "imported_at",
    "import_date",
    "notes_last_contacted",
    "notes_last_updated",
    "hs_last_sales_activity_timestamp",
)
_MEETING_DATE_KEYS = (
    "held_at",
    "fireflies_at",
    "cube_at",
    "meeting_held_at",
    "occurred_at",
    "gmail_held_at",
    "followup_meeting_at",
    "gmail_followup_at",
    "gmail_booking_at",
    "booking_at",
    "calendly_at",
    "hs_meeting_start",
    "meeting_at",
    "call_at",
    "engagements_last_meeting_booked",
    "last_meeting_at",
)
_FOLLOWUP_INVITE_RE = re.compile(r"salesglider\s+follow[- ]?up", re.I)
_WEB_BOOKING_RE = re.compile(r"salesglider\s+web\s+booking", re.I)
_DECK_OR_RECAP_RE = re.compile(
    r"growth playbook|\bplaybook\b|\bdeck\b|meeting recap|your meeting recap",
    re.I,
)
_DECLINED_INVITE_RE = re.compile(
    r"^\s*(?:fwd:\s*|re:\s*)*(?:declined|canceled|cancelled|tentative[- ]declined)\b",
    re.I,
)
_ACCEPTED_INVITE_RE = re.compile(r"^\s*(?:fwd:\s*|re:\s*)*accepted:", re.I)
_BOOKING_INVITE_RE = re.compile(
    r"\b(new event|web booking|invitee|invitation:)\b",
    re.I,
)
NURTURE_THREAD_PROP = "nurture_thread_id"
NURTURE_SUBJECT_PROP = "nurture_thread_subject"
OPENER_MAX_WORDS = 20
_TRANSCRIPT_FIRST_PERSON_RE = re.compile(
    r"\b(i|i'm|i’m|i'll|i’ll|i'd|i’d|i've|i’ve|lets|let's|let’s)\b",
    re.I,
)
_SYNTHETIC_SOURCE_ID_RE = re.compile(
    r"^(?:fireflies|cube_acr|cube|allo)(?::\d{4}-\d{2}-\d{2})?$",
    re.I,
)
_PREFIXED_MEETING_ID_RE = re.compile(r"^(?:ff|cube)-[A-Za-z0-9_-]+$", re.I)
_RAW_MEETING_ID_RE = re.compile(r"^[A-Za-z0-9_-]{12,}$")
_FOCUS_LANGUAGE_RE = re.compile(
    r"\b(?:(?:were|was|is|are|been)\s+)?(?:focused on|focusing on)\b|"
    r"\bwants? to\b|\bwanted to\b|\bknock out\b|"
    r"\bpriority\b|\bmain (?:thing|focus|priority)\b",
    re.I,
)
_MEETING_TEXT_KEYS = (
    "action_items",
    "transcript",
    "summary",
    "meeting_summary",
    "fireflies_summary",
    "cube_summary",
    "overview",
    "shorthand_bullet",
)
_GENERIC_NOTE_KEYS = frozenset(
    {
        "last_touch_snippet",
        "description",
        "nurture_reason",
        "personal_details",
        "pain_points",
        "notes",
        "hs_note",
        "source_note",
    }
)
_WANT_TOPIC_CLAUSES = (
    ("website", "you were focused on getting the website done first"),
    ("web site", "you were focused on getting the website done first"),
    ("proposal", "you were focused on getting a proposal together"),
    ("pricing", "you were focused on getting pricing nailed down"),
    ("retainer", "you were focused on the retainer"),
    ("hiring", "you were focused on hiring"),
    ("sdr", "you were focused on replacing the SDR"),
    ("cut 80", "you were focused on cutting the grind"),
    ("cut outbound", "you were focused on cutting outbound work"),
    ("80%", "you were focused on cutting the grind"),
)
KNOWN_NURTURE_REASONS = frozenset(
    {"met", "booked", "kicked_can", "no_show", "never_booked", "timing_later"}
)
_BARE_DOMAIN_RE = re.compile(r"^[a-z0-9-]+(?:\.[a-z]{2,})+$", re.I)
_FIT_NOTE_RE = re.compile(r"^fit\s*:\s*", re.I)
INDUSTRY_SUBJECTS = {row["key"]: row["subject"] for row in VERTICALS}
INDUSTRY_SUBJECTS["financial_advisors"] = "Advisor update"

ACTION_APPROVE = "nurture_approve_send"
ACTION_EDIT = "nurture_edit_send"
ACTION_REMOVE = "nurture_remove"
VIEW_EDIT = "nurture_edit_modal"

_QUOTE_CUT = re.compile(
    r"\nOn .{0,200}wrote:\s*|\n-{2,}\s*Original Message\b|\nFrom:\s|\n>+",
    re.I,
)
_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
_CANDIDATE_CAMPAIGN_RE = re.compile(r"\bcandidates?\b", re.I)
_FAMILY_RE = re.compile(
    r"\b(wife|husband|spouse|son|daughter|kids?|child(?:ren)?|family|mom|dad|"
    r"brother|sister|girlfriend|boyfriend)\b",
    re.I,
)
_DEAL_NAME_RE = re.compile(
    r"^[A-Za-z][A-Za-z'’.\-]*(?:\s+[A-Za-z][A-Za-z'’.\-]*){0,3}\s+[-–—]\s+\S+",
)
_PERSON_NAME_RE = re.compile(r"\b([A-Za-z][A-Za-z'’.\-]{1,20})\s+([A-Za-z][A-Za-z'’.\-]{1,30})\b")
_CRM_FIELD_RE = re.compile(
    r"^(dealname|deal name|deal:|company:|source:|hs_|crm_|pipeline)\b",
    re.I,
)
_STAGE_PIPELINE_NAMES = frozenset(
    {
        "nurture",
        "initial interest",
        "meeting booked",
        "discovery",
        "discovery held",
        "discovery completed",
        "discovery scheduled",
        "proposal sent",
        "needs stakeholder approval",
        "stakeholder approval",
        "poc",
        "closed won",
        "closed lost",
        "sales pipeline",
        "pipeline",
        "appointmentscheduled",
        "qualifiedtobuy",
        "presentationscheduled",
        "decisionmakerboughtin",
    }
)
_MEETING_RECAP_RE = re.compile(r"your meeting recap", re.I)
_NAME_STOP = frozenset(
    {
        "yes",
        "send",
        "info",
        "spring",
        "slow",
        "season",
        "check",
        "back",
        "after",
        "busy",
        "maybe",
        "later",
        "this",
        "year",
        "need",
        "lost",
        "just",
        "interested",
        "timing",
        "until",
        "ping",
        "then",
        "said",
        "they",
        "are",
        "slammed",
        "through",
        "summer",
        "fall",
        "about",
        "filling",
        "shoulder",
        "proposal",
        "sitting",
        "activity",
        "since",
        "august",
        "concrete",
        "talk",
        "pour",
        "fill",
        "seats",
        "before",
        "our",
        "the",
        "and",
        "but",
        "for",
        "you",
        "mentioned",
        "call",
        "from",
        "with",
        "your",
        "been",
        "few",
        "months",
        "connected",
    }
)


@dataclass
class NurtureDraft:
    subject: str
    body: str
    valid: bool = True
    reject_reason: str = ""
    spoken_source_id: str = ""
    meeting_source_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "body": self.body,
            "valid": self.valid,
            "reject_reason": self.reject_reason,
            "spoken_source_id": self.spoken_source_id,
            "meeting_source_id": self.meeting_source_id,
        }


def strip_quoted_text(body: str) -> str:
    text = (body or "").strip()
    if not text:
        return ""
    cut = _QUOTE_CUT.split(text, maxsplit=1)[0]
    lines = [ln for ln in cut.splitlines() if not ln.strip().startswith(">")]
    return "\n".join(lines).strip()


def snippet_of(text: str, limit: int = SNIPPET_MAX) -> str:
    clean = strip_quoted_text(text)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean[:limit]


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def next_fire_at_from_signal(signal_at, now: datetime | None = None) -> datetime:
    now = _aware(now or now_utc())
    signal = parse_signal_at(signal_at)
    if not signal:
        raise ValueError("no_signal_date")
    return signal + timedelta(days=TICKER_DAYS)


def is_weekday(dt: datetime) -> bool:
    local = _aware(dt).astimezone(CDT)
    return local.weekday() < 5


def next_weekday(dt: datetime) -> datetime:
    local = _aware(dt).astimezone(CDT)
    while local.weekday() >= 5:
        local += timedelta(days=1)
    return local


def first_weekday_after(day) -> datetime:
    if isinstance(day, datetime):
        local = _aware(day).astimezone(CDT)
        start = datetime(local.year, local.month, local.day, tzinfo=CDT) + timedelta(days=1)
    else:
        start = datetime.fromisoformat(str(day)).replace(tzinfo=CDT) + timedelta(days=1)
    return next_weekday(start)


def spread_past_due(
    signals: list,
    *,
    now: datetime,
    approval_date=None,
    max_per_weekday: int = NURTURE_MAX_PER_WEEKDAY,
) -> list[datetime]:
    """Oldest signal first into weekday slots starting the first weekday after approval."""
    start = first_weekday_after(approval_date or now.astimezone(CDT).date())
    ordered = sorted(parse_signal_at(s) or now for s in signals)
    slots: dict[str, int] = {}
    out: list[datetime] = []
    cursor = datetime(start.year, start.month, start.day, tzinfo=CDT)
    for _ in ordered:
        while True:
            cursor = next_weekday(cursor)
            key = cursor.date().isoformat()
            if slots.get(key, 0) < max_per_weekday:
                slots[key] = slots.get(key, 0) + 1
                out.append(cursor)
                break
            cursor += timedelta(days=1)
    return out


def roll_to_weekday_slot(
    when: datetime,
    occupied: dict[str, int] | None = None,
    max_per_weekday: int = NURTURE_MAX_PER_WEEKDAY,
) -> datetime:
    occupied = occupied if occupied is not None else {}
    cursor = next_weekday(_aware(when).astimezone(CDT))
    while True:
        key = cursor.date().isoformat()
        if occupied.get(key, 0) < max_per_weekday:
            occupied[key] = occupied.get(key, 0) + 1
            return cursor
        cursor = next_weekday(cursor + timedelta(days=1))


def is_not_deal_candidate(
    name: str = "",
    email: str = "",
    company: str = "",
    campaign: str = "",
    phone: str = "",
    contact: dict | None = None,
    deals: list | None = None,
    company_deals: list | None = None,
    extra: dict | None = None,
) -> str:
    from crmbrain.policy import exclude_reason_for_nurture_or_deal

    if is_josh_address(email):
        return "non_deal"
    if is_nurture_excluded(name=name, email=email):
        return "non_deal"
    blocked = exclude_reason_for_nurture_or_deal(
        name=name,
        email=email,
        company=company,
        phone=phone,
        title=campaign,
        contact=contact,
        deals=deals,
        company_deals=company_deals,
        extra=extra,
    )
    if blocked:
        return blocked
    if is_non_deal_person(name=name, email=email, company=company, phone=phone):
        return "non_deal"
    if is_client_context(name=name, company=company, title=campaign):
        return "client"
    if campaign and _CANDIDATE_CAMPAIGN_RE.search(campaign):
        return "non_deal"
    return ""


def is_nurture_excluded(*, name: str = "", email: str = "") -> bool:
    """Partners Josh handles personally — nurture cards only, not HubSpot deal sync."""
    email_l = (email or "").strip().lower()
    if email_l and email_l in NURTURE_EXCLUDE_EMAILS:
        return True
    blob = f"{name or ''} {email_l}".strip().lower()
    return any(token and token in blob for token in NURTURE_EXCLUDE_NAMES)


def _row_extra(row: dict | None) -> dict:
    row = row or {}
    extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
    return extra


def _has_held_meeting_source(row: dict | None) -> bool:
    extra = _row_extra(row)
    source = str(
        extra.get("meeting_source")
        or extra.get("evidence_source")
        or extra.get("source")
        or (row or {}).get("source")
        or extra.get("crm_source")
        or ""
    ).lower()
    if source in HELD_MEETING_SOURCES:
        return True
    return bool(
        extra.get("fireflies")
        or extra.get("fireflies_id")
        or extra.get("cube_acr")
        or extra.get("cube")
        or extra.get("cube_id")
        or extra.get("cube_recording")
        or extra.get("meeting_held")
        or (row or {}).get("fireflies_id")
        or (row or {}).get("cube_id")
        or (row or {}).get("fireflies")
        or (row or {}).get("cube_acr")
    )


def is_real_meeting_id(value: str | None) -> bool:
    """True for a Fireflies meeting id or Cube file id. Rejects fireflies / fireflies:<date>."""
    val = str(value or "").strip()
    if not val or val.lower() in HELD_MEETING_SOURCES:
        return False
    if _SYNTHETIC_SOURCE_ID_RE.fullmatch(val):
        return False
    if ":" in val:
        return False
    if _PREFIXED_MEETING_ID_RE.fullmatch(val):
        return True
    return bool(_RAW_MEETING_ID_RE.fullmatch(val))


def _as_meeting_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = [_as_meeting_text(item) for item in value]
        return "\n".join(part for part in parts if part)
    if isinstance(value, dict):
        parts = [
            _as_meeting_text(value.get(key))
            for key in ("overview", "shorthand_bullet", "action_items", "text", "body", "summary")
        ]
        return "\n".join(part for part in parts if part)
    return str(value).strip()


def meeting_source_id(row: dict | None) -> str:
    """Actual Fireflies/Cube meeting id only. Never fireflies, fireflies:<date>, or notes."""
    extra = _row_extra(row)
    for key in (
        "fireflies_id",
        "cube_acr_id",
        "cube_id",
        "meeting_source_id",
        "held_meeting_id",
        "source_id",
        "external_id",
    ):
        val = _as_meeting_text((row or {}).get(key) or extra.get(key))
        if is_real_meeting_id(val):
            return val
    return ""


def held_meeting_text(row: dict | None) -> str:
    """Summary / action items / transcript from that meeting. Generic deal notes do not count."""
    if not _has_held_meeting_source(row) and not meeting_source_id(row):
        return ""
    extra = _row_extra(row)
    parts: list[str] = []
    seen: set[str] = set()
    for key in _MEETING_TEXT_KEYS:
        text = _as_meeting_text((row or {}).get(key) or extra.get(key))
        if not text or key in _GENERIC_NOTE_KEYS or _FIT_NOTE_RE.match(text):
            continue
        if text in seen:
            continue
        seen.add(text)
        parts.append(text)
    nested = extra.get("summary") if isinstance(extra.get("summary"), dict) else None
    row_summary = (row or {}).get("summary")
    if isinstance(row_summary, dict):
        nested = row_summary
    if nested:
        nested_text = _as_meeting_text(nested)
        if nested_text and nested_text not in seen and not _FIT_NOTE_RE.match(nested_text):
            parts.append(nested_text)
    return "\n".join(parts)


def extract_spoken_want(text: str) -> tuple[str, float]:
    """Josh-voice want clause plus confidence. Lone keywords in a long summary are low."""
    raw = _FIT_NOTE_RE.sub("", text or "").strip()
    if not raw:
        return "", 0.0
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", raw) if s.strip()]
    focused = [s for s in sentences if _FOCUS_LANGUAGE_RE.search(s)]
    for sent in focused:
        low = sent.lower()
        for needle, clause in _WANT_TOPIC_CLAUSES:
            if needle in low:
                if spoken_clause_is_raw_transcript(clause):
                    return "", 0.0
                return clause, 0.85
        want = re.search(r"wants? to ([^.]+)", sent, flags=re.I)
        if want:
            clause = f"you wanted to {want.group(1).strip()}"
            if not spoken_clause_is_raw_transcript(clause):
                return clause, 0.8
    low_all = raw.lower()
    for needle, _clause in _WANT_TOPIC_CLAUSES:
        if needle in low_all:
            return "", 0.3
    return "", 0.0


def grounded_spoken_want(row: dict | None) -> tuple[str, str]:
    """Want clause from that meeting's own summary/action items, plus the real meeting id."""
    source_id = meeting_source_id(row)
    text = held_meeting_text(row)
    if not source_id or not text:
        return "", source_id
    clause, confidence = extract_spoken_want(text)
    if confidence < WANT_CONFIDENCE_MIN or not clause or spoken_clause_is_raw_transcript(clause):
        return "", source_id
    return clause, source_id


def held_near_booked_date(row: dict | None, *, window_days: int = HELD_NEAR_DAYS) -> bool:
    """True when Fireflies/Cube/recap shows a held meeting on or near the booked date."""
    extra = _row_extra(row)
    blob = " ".join(
        str(extra.get(k) or (row or {}).get(k) or "")
        for k in ("last_touch_snippet", "gmail_subject", "original_subject", "subject", "snippet")
    )
    recap = bool(_MEETING_RECAP_RE.search(blob))
    if not _has_held_meeting_source(row) and not recap:
        return False
    booked = parse_signal_at(
        (row or {}).get("meeting_at")
        or extra.get("meeting_at")
        or extra.get("engagements_last_meeting_booked")
        or extra.get("last_meeting_at")
    )
    held = parse_signal_at(
        extra.get("held_at")
        or extra.get("fireflies_at")
        or extra.get("cube_at")
        or extra.get("meeting_held_at")
        or extra.get("occurred_at")
    )
    if booked and held:
        return abs((held.date() - booked.date()).days) <= window_days
    return True


def has_meeting_qualification(
    *,
    source: str = "",
    reason: str = "",
    ev: Engagement | None = None,
    extra: dict | None = None,
    deal_stage: str = "",
    booked: bool = False,
    met: bool = False,
) -> bool:
    """Josh: only people who met with him or booked a real meeting (even a no-show)."""
    extra = extra or {}
    if extra.get("met") or extra.get("booked") or extra.get("has_meeting") or met or booked:
        return True
    if reason in {"no_show"}:
        return True
    if deal_stage in {
        STAGE["meeting_booked"],
        STAGE["discovery_held"],
        STAGE["proposal_sent"],
        STAGE["needs_stakeholder_approval"],
        STAGE["nurture"],
    }:
        return True
    if ev is not None:
        if ev.source in MEETING_ENROLL_SOURCES and (is_meeting_held(ev) or is_meeting_scheduled(ev)):
            return True
        if (ev.extra or {}).get("met") or (ev.extra or {}).get("booked"):
            return True
    if source in MEETING_ENROLL_SOURCES:
        return True
    return False


def may_enroll_from_engagement(ev: Engagement, reason: str = "") -> tuple[bool, str]:
    blocked = is_not_deal_candidate(
        name=ev.display_name() or ev.name,
        email=ev.email,
        company=ev.company,
        campaign=str((ev.extra or {}).get("campaign_name") or ev.raw_subject or ""),
        phone=ev.phone,
        extra=ev.extra or {},
    )
    if blocked:
        return False, blocked
    if has_meeting_qualification(
        source=ev.source,
        ev=ev,
        extra=ev.extra or {},
        reason=reason or ev.ticker_reason,
    ):
        return True, ""
    return False, "reply_only"


def candidate_merge_key(c: TickerCandidate) -> str:
    email = (c.email or "").strip().lower()
    if email:
        return f"email:{email}"
    if c.hs_contact_id:
        return f"hs:{c.hs_contact_id}"
    return f"name:{(c.name or '').strip().lower()}"


def merge_candidates(candidates: list[TickerCandidate]) -> list[TickerCandidate]:
    """One row per email (else hs id). Newest signal wins; campaign from S1 then S2 then S3."""
    order = {"smartlead": 0, "hubspot": 1, "gmail": 2}
    kept: dict[str, TickerCandidate] = {}
    for raw in candidates:
        key = candidate_merge_key(raw)
        prev = kept.get(key)
        if prev is None:
            kept[key] = raw
            continue
        prev_ts = parse_signal_at(prev.last_signal) or datetime.min.replace(tzinfo=timezone.utc)
        new_ts = parse_signal_at(raw.last_signal) or datetime.min.replace(tzinfo=timezone.utc)
        winner = raw if new_ts >= prev_ts else prev
        other = prev if winner is raw else raw
        if not (winner.extra.get("campaign") or winner.company):
            pass
        camp = ""
        camp_id = ""
        industry = winner.extra.get("industry") or other.extra.get("industry") or ""
        for src in (prev, raw):
            if order.get(src.source, 9) == 0 and (src.extra.get("campaign") or ""):
                camp = src.extra.get("campaign") or ""
                camp_id = str(src.extra.get("campaign_id") or "")
                break
        if not camp:
            for src in (prev, raw):
                if src.extra.get("campaign"):
                    camp = src.extra["campaign"]
                    camp_id = str(src.extra.get("campaign_id") or "")
                    break
        extra = dict(winner.extra)
        extra["campaign"] = camp or extra.get("campaign") or ""
        extra["campaign_id"] = camp_id or extra.get("campaign_id") or ""
        extra["industry"] = industry
        own_email = (winner.email or "").strip().lower()
        other_email = (other.email or "").strip().lower()
        own_snip = extra.get("last_touch_snippet") or ""
        other_snip = other.extra.get("last_touch_snippet") or ""
        if own_email and other_email and own_email != other_email:
            extra["last_touch_snippet"] = own_snip
        else:
            extra["last_touch_snippet"] = own_snip or other_snip
        winner.extra = extra
        if not winner.email:
            winner.email = other.email
        if not winner.company:
            winner.company = other.company
        kept[key] = winner
    return list(kept.values())


def infer_industry_resolved(
    *,
    email: str = "",
    company: str = "",
    campaign: str = "",
    website_text: str = "",
    hs_industry: str = "",
) -> tuple[str | None, str | None]:
    """First matching basis: campaign → domain → website. Record the basis."""
    from crmbrain.ticker import infer_industry

    if campaign:
        hit = infer_industry(campaign, extras={"campaign": campaign, "campaign_name": campaign})
        if hit:
            return hit["key"], "campaign"
    domain = ""
    if email and "@" in email:
        domain = email.rsplit("@", 1)[-1]
    if domain:
        hit = infer_industry(domain.replace("-", " ").replace(".", " "))
        if hit:
            return hit["key"], "domain"
        compact = re.sub(r"\.(com|net|org|io|co|us|biz)$", "", domain.lower())
        compact = compact.replace("-", "").replace(".", "")
        from crmbrain.ticker import VERTICALS as _VERTS

        for row in _VERTS:
            for kw in row["keywords"]:
                token = kw.replace(" ", "")
                if token and token in compact:
                    return row["key"], "domain"
    if hs_industry:
        hit = infer_industry("", industry=hs_industry)
        if not hit:
            hit = infer_industry(hs_industry)
        if hit:
            return hit["key"], "website"
    if website_text:
        hit = infer_industry(website_text, extras={"website_text": website_text})
        if hit:
            return hit["key"], "website"
    if company:
        hit = infer_industry(company)
        if hit:
            return hit["key"], "website"
    return None, None


def _first_name(name: str) -> str:
    raw = (name or "").strip().split(" ")[0] or "there"
    if raw.lower() == "there":
        return "there"
    if raw.isupper() or raw.islower() or raw[:1].islower():
        return raw[:1].upper() + raw[1:].lower()
    return raw[:1].upper() + raw[1:]


def _own_name_tokens(row: dict | None) -> set[str]:
    text = " ".join(
        str((row or {}).get(k) or "")
        for k in ("name", "first_name", "last_name", "email")
    )
    tokens = {p.lower() for p in re.split(r"[^A-Za-z']+", text) if len(p) > 1}
    email = str((row or {}).get("email") or "")
    if "@" in email:
        local = email.split("@", 1)[0]
        tokens.update(p.lower() for p in re.split(r"[._\-]+", local) if len(p) > 1)
    return tokens


def looks_like_deal_name(snippet: str, row: dict | None = None) -> bool:
    text = (snippet or "").strip()
    if not text:
        return False
    dealname = str((row or {}).get("dealname") or (row or {}).get("deal_name") or "")
    if dealname and text.lower() == dealname.strip().lower():
        return True
    if _DEAL_NAME_RE.match(text):
        return True
    if _CRM_FIELD_RE.match(text):
        return True
    return False


def snippet_mentions_other_person(snippet: str, row: dict | None = None) -> bool:
    """True when the snippet names a person who is not this contact."""
    own = _own_name_tokens(row)
    if not own:
        return False
    for match in _PERSON_NAME_RE.finditer(snippet or ""):
        raw_first, raw_last = match.group(1), match.group(2)
        first, last = raw_first.lower(), raw_last.lower()
        if first in _NAME_STOP or last in _NAME_STOP:
            continue
        if len(first) < 3 or len(last) < 4:
            continue
        if not raw_last[:1].isupper():
            continue
        if first in own or last in own:
            continue
        return True
    return False


def _strip_crm_prefix(text: str) -> str:
    out = _FIT_NOTE_RE.sub("", text or "").strip()
    out = re.sub(r"^source:\s*[^.]+\.\s*", "", out, flags=re.I).strip()
    if _CRM_FIELD_RE.match(out) and len(out.split()) < 8:
        return ""
    return out


def _norm_topic(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def is_stage_or_pipeline_name(text: str) -> bool:
    low = _norm_topic(text)
    return bool(low) and low in _STAGE_PIPELINE_NAMES


def is_self_or_company_topic(text: str, row: dict | None = None) -> bool:
    """True when text is just this contact's name or the company-name field."""
    low = _norm_topic(text)
    if not low:
        return False
    row = row or {}
    name = _norm_topic(str(row.get("name") or ""))
    company = _norm_topic(str(row.get("company") or ""))
    first = _norm_topic(str(row.get("first_name") or ""))
    last = _norm_topic(str(row.get("last_name") or ""))
    if name and (low == name or low.replace(" ", "") == name.replace(" ", "")):
        return True
    parts = [p for p in name.split() if p]
    if parts and low == parts[0]:
        return True
    if len(parts) > 1 and low == " ".join(parts[-2:]):
        return True
    if len(parts) > 1 and low == parts[-1] and len(parts[-1]) > 2:
        return True
    if first and last and low == f"{first} {last}":
        return True
    if company and (low == company or low.replace(" ", "") == company.replace(" ", "")):
        return True
    return False


def is_banned_opener_topic(text: str, row: dict | None = None) -> bool:
    if not (text or "").strip():
        return True
    if is_stage_or_pipeline_name(text):
        return True
    if is_self_or_company_topic(text, row):
        return True
    return False


def is_usable_speech_snippet(snippet: str, row: dict | None = None) -> bool:
    text = _strip_crm_prefix(snippet_of(snippet or ""))
    if _FIT_NOTE_RE.match((snippet or "").strip()) or _FIT_NOTE_RE.match(text):
        return False
    if len(text) < 8:
        return False
    if _FAMILY_RE.search(text):
        return False
    if looks_like_deal_name(text, row):
        return False
    if snippet_mentions_other_person(text, row):
        return False
    if is_banned_opener_topic(text, row):
        return False
    return True


def scoped_snippet(snippet: str, row: dict | None = None) -> str:
    """Keep only this contact's own speech. Drop deal names, family, other people."""
    text = snippet_of(snippet or "")
    if not is_usable_speech_snippet(text, row):
        return ""
    return text


def _no_show_count_of(extra: dict | None) -> int:
    extra = extra or {}
    raw = extra.get("no_show_count") or extra.get("hs_no_show_count")
    if raw in (None, ""):
        props = extra.get("properties") if isinstance(extra.get("properties"), dict) else {}
        raw = props.get("no_show_count")
    try:
        return int(float(raw or 0))
    except (TypeError, ValueError):
        return 0


def _same_calendar_day(left, right) -> bool:
    a = parse_signal_at(left) if not isinstance(left, datetime) else left
    b = parse_signal_at(right) if not isinstance(right, datetime) else right
    if not a or not b:
        return False
    if a.tzinfo is None:
        a = a.replace(tzinfo=timezone.utc)
    if b.tzinfo is None:
        b = b.replace(tzinfo=timezone.utc)
    return a.astimezone(timezone.utc).date() == b.astimezone(timezone.utc).date()


def _forbidden_meeting_stamps(row: dict | None) -> list[datetime]:
    extra = _row_extra(row)
    props = extra.get("properties") if isinstance(extra.get("properties"), dict) else {}
    out: list[datetime] = []
    for src in ((row or {}), extra, props):
        if not isinstance(src, dict):
            continue
        for key in _FORBIDDEN_DATE_KEYS:
            dt = parse_signal_at(src.get(key))
            if dt:
                out.append(dt)
    return out


def _iter_gmail_messages(row: dict | None) -> list[dict]:
    extra = _row_extra(row)
    raw = (row or {}).get("gmail_messages") or extra.get("gmail_messages") or []
    if isinstance(raw, dict):
        raw = [raw]
    return [item for item in raw if isinstance(item, dict)]


def _sender_is_josh(sender: str) -> bool:
    blob = sender or ""
    emails = re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+", blob)
    if any(is_josh_address(email) for email in emails):
        return True
    return "salesglidergrowth.com" in blob.lower()


def _message_meeting_at(msg: dict) -> datetime | None:
    from crmbrain.sources.gmail_scan import parse_ics_dtstart, parse_meeting_at

    subject = str(msg.get("subject") or "")
    body = str(msg.get("body") or msg.get("snippet") or msg.get("ics") or "")
    stamp = parse_meeting_at(subject, body)
    if stamp:
        return stamp
    ics = parse_ics_dtstart(str(msg.get("ics") or body))
    if ics:
        return ics
    return parse_signal_at(msg.get("meeting_at") or msg.get("held_at") or msg.get("date"))


def invite_is_rejected(subject: str = "", msg: dict | None = None) -> bool:
    """Declined / canceled / tentative-declined invites never count as booked or held."""
    text = str(subject or "")
    if msg:
        text = f"{text} {msg.get('subject') or ''}"
        if msg.get("declined") or msg.get("canceled") or msg.get("cancelled"):
            return True
        status = str(msg.get("status") or msg.get("response") or "").lower()
        if status in {"declined", "canceled", "cancelled", "tentative-declined"}:
            return True
    return bool(_DECLINED_INVITE_RE.search(text.strip()))


def chicago_meeting_date(dt: datetime) -> datetime:
    """America/Chicago calendar date of the meeting start, as midnight CDT."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CDT)
    utc = dt.astimezone(timezone.utc)
    if utc.hour == 0 and utc.minute == 0 and utc.second == 0 and utc.microsecond == 0:
        day = utc.date()
    else:
        day = dt.astimezone(CDT).date()
    return datetime(day.year, day.month, day.day, tzinfo=CDT)


def chicago_date_phrase(dt: datetime) -> str:
    return chicago_meeting_date(dt).strftime("%b %-d")


def _latest(stamps: list[datetime], *, now: datetime | None = None) -> datetime | None:
    now = now or now_utc()
    past = [s for s in stamps if s and s <= now]
    return max(past) if past else None


def gmail_meeting_evidence(row: dict | None, *, now: datetime | None = None) -> dict[str, Any]:
    """Booking/held dates from Gmail. Declined and future invites do not count as held."""
    now = now or now_utc()
    extra = _row_extra(row)
    recaps: list[datetime] = []
    accepted_held: list[datetime] = []
    bookings: list[datetime] = []
    future_bookings: list[datetime] = []
    rejected: list[datetime] = []
    structured_followup = parse_signal_at(
        (row or {}).get("gmail_followup_at") or extra.get("gmail_followup_at")
    )
    structured_booking = parse_signal_at(
        (row or {}).get("gmail_booking_at")
        or extra.get("gmail_booking_at")
        or (row or {}).get("calendly_at")
        or extra.get("calendly_at")
    )
    followup_subj = str(
        (row or {}).get("gmail_followup_subject") or extra.get("gmail_followup_subject") or ""
    )
    booking_subj = str(
        (row or {}).get("gmail_booking_subject") or extra.get("gmail_booking_subject") or ""
    )
    deck_subj = str((row or {}).get("gmail_deck_subject") or extra.get("gmail_deck_subject") or "")
    if (
        (row or {}).get("gmail_deck")
        or extra.get("gmail_deck")
        or (row or {}).get("gmail_recap")
        or extra.get("gmail_recap")
        or _DECK_OR_RECAP_RE.search(deck_subj)
        or _MEETING_RECAP_RE.search(deck_subj)
    ):
        recap_at = parse_signal_at(
            (row or {}).get("gmail_recap_at") or extra.get("gmail_recap_at") or structured_followup
        )
        if recap_at:
            recaps.append(recap_at)
    if structured_followup and not invite_is_rejected(followup_subj):
        if _ACCEPTED_INVITE_RE.search(followup_subj) or (row or {}).get("gmail_followup_accepted") or extra.get(
            "gmail_followup_accepted"
        ):
            if structured_followup <= now:
                accepted_held.append(structured_followup)
            else:
                future_bookings.append(structured_followup)
    if structured_booking and not invite_is_rejected(booking_subj):
        if structured_booking <= now:
            bookings.append(structured_booking)
        else:
            future_bookings.append(structured_booking)
    for msg in _iter_gmail_messages(row):
        subject = str(msg.get("subject") or "")
        body = str(msg.get("body") or msg.get("snippet") or "")
        blob = f"{subject} {body}"
        if invite_is_rejected(subject, msg):
            stamp = _message_meeting_at(msg)
            if stamp:
                rejected.append(stamp)
            continue
        stamp = _message_meeting_at(msg)
        if _sender_is_josh(str(msg.get("from") or msg.get("sender") or "")) and (
            _DECK_OR_RECAP_RE.search(blob) or _MEETING_RECAP_RE.search(blob)
        ):
            recap_stamp = parse_signal_at(msg.get("date")) or stamp
            if recap_stamp and recap_stamp <= now:
                recaps.append(recap_stamp)
            continue
        if not stamp:
            continue
        accepted = bool(_ACCEPTED_INVITE_RE.search(subject) or msg.get("accepted"))
        bookingish = bool(
            _WEB_BOOKING_RE.search(blob)
            or _BOOKING_INVITE_RE.search(blob)
            or "calendly" in blob.lower()
            or "salesglider" in blob.lower()
        )
        if stamp > now:
            if accepted or bookingish:
                future_bookings.append(stamp)
            continue
        if accepted:
            accepted_held.append(stamp)
        elif bookingish:
            bookings.append(stamp)
    recap_at = _latest(recaps, now=now)
    held_invite_at = _latest(accepted_held, now=now)
    booking_at = _latest(bookings, now=now)
    held_at = recap_at or held_invite_at
    met = bool(held_at)
    booked = bool(booking_at or held_at)
    source = "gmail_followup" if held_at else "gmail_booking" if booking_at else ""
    return {
        "met": met,
        "booked": booked,
        "held_at": held_at,
        "recap_at": recap_at,
        "booking_at": booking_at,
        "source": source,
        "future_booking": bool(future_bookings),
        "rejected_at": rejected,
    }


def lookup_gmail_meeting_messages(gmail, *, email: str) -> list[dict]:
    """Read booking / follow-up / playbook mail for this contact. Never used as a reply thread."""
    addr = str(email or "").strip()
    if gmail is None or not addr or "@" not in addr:
        return []
    search = getattr(gmail, "search", None)
    if not callable(search):
        return []
    query = (
        f"(from:{addr} OR to:{addr}) "
        f'(SalesGlider OR Calendly OR "Growth Playbook" OR playbook OR "meeting recap" '
        f'OR "Web Booking" OR Followup OR Follow-up OR "Accepted:" OR "Invitation:" OR "New Event") '
        f"-subject:Declined -subject:Canceled -subject:Cancelled"
    )
    try:
        stubs = search(query, max_results=15) or []
    except Exception as exc:
        logger.warning("gmail meeting evidence search failed: %s", exc)
        return []
    getter = getattr(gmail, "get", None)
    out: list[dict] = []
    for stub in stubs:
        if not isinstance(stub, dict):
            continue
        mid = str(stub.get("id") or "")
        subject = str(stub.get("subject") or "")
        body = str(stub.get("body") or stub.get("snippet") or "")
        sender = str(stub.get("from") or stub.get("sender") or "")
        ics = str(stub.get("ics") or "")
        fetched = stub
        if callable(getter) and mid:
            try:
                fetched = getter(mid)
            except Exception as exc:
                logger.warning("gmail meeting evidence get %s failed: %s", mid, exc)
                fetched = stub
            headers = {}
            if hasattr(gmail, "headers_map"):
                try:
                    headers = gmail.headers_map(fetched) or {}
                except Exception:
                    headers = {}
            subject = subject or str(headers.get("subject") or "")
            sender = sender or str(headers.get("from") or "")
            if hasattr(gmail, "body_text"):
                try:
                    body = body or str(gmail.body_text(fetched) or "")
                except Exception:
                    pass
            body = body or str((fetched or {}).get("snippet") or "")
            if hasattr(gmail, "calendar_parts"):
                try:
                    ics = ics or "\n".join(gmail.calendar_parts(fetched) or [])
                except Exception:
                    pass
        if ics and ics not in body:
            body = f"{body}\n{ics}".strip()
        if subject or body:
            out.append({"id": mid, "subject": subject, "body": body, "from": sender, "ics": ics})
    return out


def gmail_client_for_cards(settings: Settings | None, gmail=None):
    """Use the passed Gmail client, or build one from settings for --sample-cards / live cards."""
    if gmail is not None:
        return gmail
    if not settings or not getattr(settings, "gmail_refresh_token", ""):
        return None
    try:
        from crmbrain.gmail_client import Gmail

        return Gmail(settings)
    except Exception as exc:
        logger.warning("gmail client for nurture cards failed: %s", exc)
        return None


def apply_gmail_meeting_evidence(row: dict | None, gmail=None) -> dict:
    """Search Gmail and stamp booking/held dates onto the card row."""
    out = dict(row or {})
    extra = dict(_row_extra(out))
    email = str(out.get("email") or extra.get("email") or "")
    messages = list(_iter_gmail_messages(out))
    already_looked_up = "gmail_messages" in out or "gmail_messages" in extra
    fetched = (
        lookup_gmail_meeting_messages(gmail, email=email)
        if gmail is not None and email and not already_looked_up
        else []
    )
    if fetched:
        messages = fetched + [m for m in messages if m not in fetched]
    if messages:
        out["gmail_messages"] = messages
        extra["gmail_messages"] = messages
    ev = gmail_meeting_evidence({**out, "extra": extra})
    if ev.get("future_booking"):
        extra["skip_nurture"] = "future_booking"
        out["skip_nurture"] = "future_booking"
    recording = bool(_has_held_meeting_source(out) or meeting_source_id(out))
    if ev.get("held_at") and not recording:
        iso = ev["held_at"].isoformat()
        extra["held_at"] = iso
        extra["gmail_followup_at"] = iso
        extra["met"] = True
        extra["meeting_date_source"] = "gmail_followup"
        out["held_at"] = iso
        out["gmail_followup_at"] = iso
        out["met"] = True
        out["meeting_date_source"] = "gmail_followup"
    elif ev.get("held_at") and recording:
        extra["met"] = True
        out["met"] = True
    elif ev.get("booking_at") and not recording:
        iso = ev["booking_at"].isoformat()
        extra["gmail_booking_at"] = iso
        extra["booked"] = True
        extra["meeting_date_source"] = extra.get("meeting_date_source") or ev.get("source") or "gmail_booking"
        if not out.get("meeting_at"):
            extra["meeting_at"] = iso
            out["meeting_at"] = iso
        out["gmail_booking_at"] = iso
        out["booked"] = True
        out["meeting_date_source"] = extra["meeting_date_source"]
    out["extra"] = extra
    return out


def meeting_evidence_from_extra(extra: dict | None) -> tuple[bool, bool]:
    """Derive (met, booked) from Fireflies/Cube, recap, calendar, HS meeting, Gmail, no-show count."""
    extra = extra or {}
    blob = " ".join(
        str(extra.get(k) or "")
        for k in (
            "last_touch_snippet",
            "gmail_subject",
            "original_subject",
            "subject",
            "snippet",
            "source_note",
        )
    )
    source = str(
        extra.get("meeting_source") or extra.get("evidence_source") or extra.get("source") or ""
    )
    held_source = source in {"fireflies", "cube_acr", "cube", "allo"}
    gmail_ev = gmail_meeting_evidence(extra)
    met = bool(
        extra.get("met")
        or extra.get("meeting_held")
        or extra.get("past_meeting")
        or extra.get("fireflies")
        or extra.get("fireflies_id")
        or extra.get("cube_acr")
        or extra.get("cube")
        or extra.get("cube_recording")
        or held_source
        or gmail_ev.get("met")
        or _MEETING_RECAP_RE.search(blob)
    )
    booked = bool(
        extra.get("booked")
        or extra.get("has_meeting")
        or extra.get("hs_meeting")
        or extra.get("hs_meeting_id")
        or extra.get("meeting_engagement")
        or extra.get("calendar_event")
        or extra.get("meeting_at")
        or extra.get("engagements_last_meeting_booked")
        or extra.get("last_meeting_at")
        or gmail_ev.get("booked")
        or _no_show_count_of(extra) > 0
    )
    return met, booked


def infer_nurture_reason(
    *,
    reason: str = "",
    deal_stage: str = "",
    extra: dict | None = None,
    booked: bool = False,
    met: bool = False,
) -> str:
    """Prefer HubSpot/Fireflies/Cube meeting evidence over stale never_booked."""
    extra = extra or {}
    stage = str(deal_stage or extra.get("deal_stage") or "")
    ev_met, ev_booked = meeting_evidence_from_extra(extra)
    met_flag = bool(met or ev_met)
    booked_flag = bool(booked or ev_booked)
    met_stages = {
        value
        for value in (
            STAGE.get("discovery_held"),
            STAGE["discovery_completed"],
            STAGE["proposal_sent"],
            STAGE.get("needs_stakeholder_approval"),
        )
        if value
    }
    booked_stages = {
        value
        for value in (
            STAGE.get("meeting_booked"),
            STAGE["discovery_scheduled"],
        )
        if value
    }
    why = (reason or "").strip()
    if why.lower() not in KNOWN_NURTURE_REASONS:
        why = ""
    # Nurture deals: derive met/booked from meeting evidence. Never keep a raw HubSpot note.
    if stage == STAGE["nurture"]:
        if why == "kicked_can":
            return "kicked_can"
        if met_flag:
            return "met"
        if booked_flag or why == "no_show":
            return "booked"
        if why in {"kicked_can", "timing_later"}:
            return "kicked_can"
        return "booked"
    if met_flag or stage in met_stages:
        if why == "kicked_can":
            return "kicked_can"
        return "met"
    if booked_flag or why == "no_show" or stage in booked_stages:
        return "booked"
    if why in {"kicked_can", "timing_later"}:
        return "kicked_can"
    return why or "never_booked"


def stored_nurture_thread_id(row: dict | None) -> str:
    """Only the thread we started. Never an old intro / calendar / third-party thread."""
    row = row or {}
    extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
    return str(
        row.get(NURTURE_THREAD_PROP)
        or extra.get(NURTURE_THREAD_PROP)
        or ""
    ).strip()


def stored_nurture_thread_subject(row: dict | None) -> str:
    row = row or {}
    extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
    return str(
        row.get(NURTURE_SUBJECT_PROP)
        or extra.get(NURTURE_SUBJECT_PROP)
        or (row.get("original_subject") if stored_nurture_thread_id(row) else "")
        or ""
    ).strip()


def attach_gmail_thread(row: dict, gmail=None) -> dict:
    """Reply only in the stored nurture thread. Never search old Gmail threads."""
    del gmail
    out = dict(row or {})
    stored = stored_nurture_thread_id(out)
    subject = stored_nurture_thread_subject(out)
    if stored:
        out[NURTURE_THREAD_PROP] = stored
        out["gmail_thread_id"] = stored
        out["thread_id"] = stored
        out["thread_kind"] = "reply"
        if subject:
            out["original_subject"] = subject
            out[NURTURE_SUBJECT_PROP] = subject
        return out
    out["gmail_thread_id"] = ""
    out["thread_id"] = ""
    out["thread_kind"] = "new_thread"
    return out


def persist_nurture_thread(hs, row: dict, thread_id: str, subject: str = "") -> None:
    """Write the new nurture thread onto the HubSpot contact and deal."""
    thread_id = str(thread_id or "").strip()
    if not thread_id or hs is None:
        return
    props = {NURTURE_THREAD_PROP: thread_id}
    if subject:
        props[NURTURE_SUBJECT_PROP] = subject
    contact_id = str(row.get("hs_contact_id") or "")
    deal_id = str(row.get("hs_deal_id") or "")
    try:
        if contact_id and hasattr(hs, "patch_contact"):
            hs.patch_contact(contact_id, props)
    except Exception as exc:
        logger.warning("nurture_thread_id contact patch failed: %s", exc)
    try:
        if deal_id and hasattr(hs, "patch_deal"):
            hs.patch_deal(deal_id, props)
    except Exception as exc:
        logger.warning("nurture_thread_id deal patch failed: %s", exc)


def _topic_from_snippet(snippet: str) -> str:
    clean = snippet_of(snippet, 80)
    clean = re.sub(r"^(yes[,.]?\s*|hey\s+\w+[,.]?\s*)", "", clean, flags=re.I)
    part = re.split(r"[.!?]", clean)[0].strip()
    part = re.sub(r"^(i |we |they |you )", "", part, flags=re.I)
    if len(part) > 52:
        part = part[:52].rsplit(" ", 1)[0]
    return part.strip(" ,")


def compose_nurture_subject(row: dict) -> str:
    """New nurture emails get a clean subject. Replies use the stored nurture subject."""
    stored = stored_nurture_thread_id(row)
    original = stored_nurture_thread_subject(row)
    if stored and original:
        return thread_reply_headers(original)["Subject"]
    company = nurture_company_label(
        str(row.get("company") or ""),
        str(row.get("dealname") or row.get("deal_name") or ""),
        str(row.get("email") or ""),
    )
    if company:
        return _no_dashes(f"{company} follow up")
    return "Following up"


def _airpods_line() -> str:
    return "I can also send you a pair of AirPods just for chatting 15 minutes to see if this makes sense."


def _ensure_sentence_period(text: str) -> str:
    out = (text or "").rstrip()
    if out and out[-1] not in ".!?":
        out += "."
    return out


def _proof_industry_key(industry: str | None) -> str:
    key = str(industry or "").strip().lower()
    if not key:
        return ""
    key = PROOF_ALIASES.get(key, key)
    if key in CASE_STUDIES:
        return key
    return ""


def _proof_line(industry: str | None) -> str:
    key = _proof_industry_key(industry)
    if key:
        return _ensure_sentence_period(CASE_STUDIES[key])
    return _ensure_sentence_period(GENERAL_PROOF)


def approved_proof_texts() -> frozenset[str]:
    """Approved proof sentences as stored and as they appear after line capitalization."""
    out: set[str] = set()
    for line in APPROVED_PROOF_LINES:
        ended = _ensure_sentence_period(line)
        out.add(ended)
        out.add(capitalize_body_lines(ended))
    return frozenset(out)


def is_bare_domain(text: str) -> bool:
    return bool(_BARE_DOMAIN_RE.fullmatch((text or "").strip()))


def display_company_name(company: str = "", dealname: str = "", email: str = "") -> str:
    """Real company label. Never a bare domain like wrsroof.com."""
    raw = (company or "").strip()
    if raw and not is_bare_domain(raw) and not is_domain_derived_company(raw, email):
        return raw
    tail = ""
    if " - " in (dealname or ""):
        tail = dealname.split(" - ", 1)[-1].strip()
    if (
        tail
        and not is_bare_domain(tail)
        and not is_domain_derived_company(tail, email)
        and _norm_topic(tail) not in _STAGE_PIPELINE_NAMES
    ):
        return tail
    return ""


def title_company_name(name: str) -> str:
    parts: list[str] = []
    for word in (name or "").split():
        if word.isupper() and 2 <= len(word) <= 5:
            parts.append(word)
        elif any(ch.isupper() for ch in word[1:]):
            parts.append(word)
        else:
            parts.append(word[:1].upper() + word[1:].lower())
    return " ".join(parts)


def is_domain_derived_company(company: str = "", email: str = "") -> bool:
    raw = (company or "").strip()
    if not raw:
        return True
    if is_bare_domain(raw):
        return True
    from crmbrain.config import email_domain

    host = email_domain(email)
    if not host:
        return False
    label = host.split(".")[0]
    return _norm_topic(raw) in {
        _norm_topic(label),
        _norm_topic(host),
        _norm_topic(host.replace(".", " ")),
    }


def nurture_company_label(company: str = "", dealname: str = "", email: str = "") -> str:
    raw = display_company_name(company, dealname, email)
    if not raw or is_domain_derived_company(raw, email):
        return ""
    return title_company_name(raw)


def _strip_poc_phrases(text: str) -> str:
    out = text or ""
    for phrase in (
        "free poc",
        "free p.o.c",
        "proof of concept",
        "proof-of-concept",
        "free 10k",
        "free 10 k",
        "10k lead",
        "free test list",
        "test list",
        "free campaign",
        "free proof",
        "free 10K",
    ):
        out = re.sub(re.escape(phrase), "", out, flags=re.I)
    out = re.sub(r"\s{2,}", " ", out).strip(" ,.-")
    return out


def _row_meeting_flags(row: dict | None) -> tuple[bool, bool]:
    """(met, booked). Nurture stage alone is not evidence of a held call."""
    row = row or {}
    extra = row.get("extra") if isinstance(row.get("extra"), dict) else row
    why = str(row.get("reason") or "").strip().lower()
    ev_met, ev_booked = meeting_evidence_from_extra(extra)
    gmail_ev = gmail_meeting_evidence(row)
    met = bool(ev_met or gmail_ev.get("met") or row.get("met") or why == "met")
    booked = bool(ev_booked or gmail_ev.get("booked") or row.get("booked") or why in {"booked", "no_show"})
    stage = str(row.get("deal_stage") or extra.get("deal_stage") or "")
    if stage in {
        STAGE.get("discovery_held"),
        STAGE["discovery_completed"],
        STAGE["proposal_sent"],
        STAGE.get("needs_stakeholder_approval"),
    }:
        met = True
    if stage in {
        STAGE.get("meeting_booked"),
        STAGE["discovery_scheduled"],
    }:
        booked = True
    if why == "kicked_can" and not met:
        booked = True
    if booked and not met and held_near_booked_date(row):
        met = True
    return met, booked


def _row_met_or_booked(row: dict | None) -> bool:
    met, booked = _row_meeting_flags(row)
    return met or booked


def _trusted_date_source(row: dict | None, key: str, explicit: str = "") -> str:
    extra = _row_extra(row)
    source = str(
        explicit
        or (row or {}).get("meeting_date_source")
        or extra.get("meeting_date_source")
        or ""
    ).lower()
    if source in TRUSTED_MEETING_DATE_SOURCES:
        return source
    if key in {
        "held_at",
        "fireflies_at",
        "cube_at",
        "meeting_held_at",
        "occurred_at",
        "gmail_held_at",
        "followup_meeting_at",
        "gmail_followup_at",
    }:
        if _has_held_meeting_source(row) or key.startswith("gmail_") or "followup" in key:
            return "fireflies" if _has_held_meeting_source(row) else "gmail_followup"
    if key in {"gmail_booking_at", "booking_at", "calendly_at"}:
        return "gmail_booking" if "gmail" in key or key == "booking_at" else "calendly"
    if key in {"hs_meeting_start", "last_meeting_at"}:
        return "hs_meeting"
    return source or key


def _recording_held_stamps(row: dict | None) -> list[datetime]:
    extra = _row_extra(row)
    if not (_has_held_meeting_source(row) or meeting_source_id(row)):
        return []
    stamps: list[datetime] = []
    for key in ("fireflies_at", "cube_at", "meeting_held_at", "occurred_at"):
        dt = parse_signal_at((row or {}).get(key) or extra.get(key))
        if dt:
            stamps.append(dt)
    source = str((row or {}).get("meeting_date_source") or extra.get("meeting_date_source") or "")
    if source in {"fireflies", "cube", "cube_acr", "allo", "hs_meeting"}:
        dt = parse_signal_at((row or {}).get("held_at") or extra.get("held_at"))
        if dt:
            stamps.append(dt)
    return stamps


def _call_date_phrase(row: dict | None) -> str:
    """Held date from Fireflies/Cube, then recap/deck, then accepted past invite. Booked uses booking only."""
    row = row or {}
    extra = _row_extra(row)
    now = now_utc()
    forbidden = _forbidden_meeting_stamps(row)
    gmail_ev = gmail_meeting_evidence(row)
    met, booked = _row_meeting_flags(row)
    dt = None
    source = ""
    if met:
        recording = _latest(_recording_held_stamps(row), now=now)
        if recording:
            dt, source = recording, "fireflies"
        elif gmail_ev.get("recap_at"):
            dt, source = gmail_ev["recap_at"], "gmail_followup"
        elif gmail_ev.get("held_at"):
            dt, source = gmail_ev["held_at"], "gmail_followup"
        else:
            for key in ("held_at", "meeting_at", "hs_meeting_start", "last_meeting_at"):
                cand = parse_signal_at(row.get(key) or extra.get(key))
                if not cand or cand > now:
                    continue
                if any(_same_calendar_day(cand, rejected) for rejected in gmail_ev.get("rejected_at") or []):
                    continue
                if any(_same_calendar_day(cand, stamp) for stamp in forbidden) and _trusted_date_source(row, key) not in TRUSTED_MEETING_DATE_SOURCES:
                    continue
                dt, source = cand, _trusted_date_source(row, key)
                break
    elif booked:
        dt = gmail_ev.get("booking_at")
        source = "gmail_booking" if dt else ""
        if not dt:
            for key in ("gmail_booking_at", "booking_at", "calendly_at", "meeting_at", "hs_meeting_start"):
                raw = row.get(key) or extra.get(key)
                cand = parse_signal_at(raw)
                if cand and cand <= now:
                    dt = cand
                    source = _trusted_date_source(row, key)
                    break
    if not dt or dt > now:
        return ""
    matches_forbidden = any(_same_calendar_day(dt, stamp) for stamp in forbidden)
    if matches_forbidden and source not in TRUSTED_MEETING_DATE_SOURCES:
        return ""
    return chicago_date_phrase(dt)


def spoken_clause_is_raw_transcript(text: str) -> bool:
    """Reject first-person I/let's or a clause longer than ~20 words."""
    clean = re.sub(r"\s+", " ", (text or "").strip())
    if not clean:
        return False
    words = [w for w in clean.split(" ") if w]
    if len(words) > OPENER_MAX_WORDS:
        return True
    return bool(_TRANSCRIPT_FIRST_PERSON_RE.search(clean))


def summarize_spoken_want(text: str) -> str:
    """One short Josh-voice clause, or empty when the meeting text is not a clear want."""
    clause, confidence = extract_spoken_want(text)
    if confidence < WANT_CONFIDENCE_MIN:
        return ""
    return clause


def capitalize_body_lines(body: str) -> str:
    """Capitalize the first letter of each line. Leave the rest unchanged."""
    out: list[str] = []
    for line in (body or "").splitlines():
        if not line.strip():
            out.append(line)
            continue
        chars = list(line)
        for i, ch in enumerate(chars):
            if ch.isalpha():
                chars[i] = ch.upper()
                break
        out.append("".join(chars))
    return "\n".join(out)


def _opener_from_snippet(first: str, snippet: str, campaign: str = "", row: dict | None = None) -> str:
    first = _first_name(first)
    row = row or {}
    spoken, _source_id = grounded_spoken_want(row)
    del snippet
    met, booked = _row_meeting_flags(row)
    date_phrase = _call_date_phrase(row)
    if met:
        if spoken:
            if date_phrase:
                return f"Hey {first}, on our {date_phrase} call {spoken}."
            return f"Hey {first}, wanted to circle back. {spoken[0].upper() + spoken[1:]}."
        if date_phrase:
            return f"Hey {first}, following up on our {date_phrase} call."
        return f"Hey {first}, wanted to circle back."
    if booked:
        if date_phrase:
            return f"Hey {first}, circling back on the {date_phrase} meeting we had booked."
        return f"Hey {first}, circling back on the call we had set up."
    topic = ""
    if campaign and not looks_like_deal_name(campaign, row) and not is_banned_opener_topic(campaign, row):
        topic = re.sub(r"salesglider\s*", "", campaign, flags=re.I).strip() or ""
    if (
        topic
        and not is_banned_opener_topic(topic, row)
        and not snippet_mentions_other_person(topic, row)
        and not _FAMILY_RE.search(topic)
    ):
        return f"Hey {first}, you replied a while back when we reached out about {topic}."
    return f"Hey {first}, it's been a few months since we connected."


def _numbers_in(text: str) -> set[str]:
    return {m.group(0).replace(",", "") for m in _NUMBER_RE.finditer(text or "")}


def compose_nurture_draft(row: dict, *, airpods: bool | None = None) -> NurtureDraft:
    """Spec draft: opener from snippet, industry proof, meeting guarantee, Josh Osborn."""
    name = str(row.get("name") or "")
    first = _first_name(name)
    company = display_company_name(
        str(row.get("company") or ""),
        str(row.get("dealname") or row.get("deal_name") or ""),
        str(row.get("email") or ""),
    )
    label = nurture_company_label(
        company,
        str(row.get("dealname") or row.get("deal_name") or ""),
        str(row.get("email") or ""),
    )
    if label:
        row = {**row, "company": label}
    industry = (row.get("industry") or "") or None
    campaign = str(row.get("campaign") or "")
    if not industry:
        industry, _basis = infer_industry_resolved(
            email=str(row.get("email") or ""),
            company=str(row.get("company") or ""),
            campaign=campaign,
            website_text=str(row.get("website_text") or ""),
            hs_industry=str(row.get("hs_industry") or ""),
        )
    raw_snippet = str(row.get("last_touch_snippet") or "")
    snippet = scoped_snippet(raw_snippet, row)
    use_airpods = AIRPODS_OFFER_LIVE if airpods is None else airpods
    spoken, spoken_source_id = grounded_spoken_want(row)
    opener = _no_dashes(_opener_from_snippet(first, raw_snippet, campaign, row))
    proof = _proof_line(industry)
    cta = MEETING_GUARANTEE
    if use_airpods:
        cta = f"{cta} {_airpods_line()}"
    body = capitalize_body_lines(
        _no_dashes(f"{opener}\n\n{proof}\n\n{cta}\n\nWorth a look?\n\nJosh Osborn")
    )
    subject = compose_nurture_subject({**row, "last_touch_snippet": snippet})
    draft = NurtureDraft(
        subject=subject,
        body=body,
        spoken_source_id=spoken_source_id if spoken else "",
        meeting_source_id=meeting_source_id(row),
    )
    return validate_draft(draft, row)


def validate_draft(draft: NurtureDraft, row: dict | None = None) -> NurtureDraft:
    body = capitalize_body_lines(_no_dashes(draft.body))
    subject = _no_dashes(draft.subject)
    draft.body = body
    draft.subject = subject
    if has_free_poc_offer(body) or has_free_poc_offer(subject):
        draft.valid = False
        draft.reject_reason = "free_poc"
        return draft
    if any(d in body or d in subject for d in ("—", "–", "−")):
        draft.valid = False
        draft.reject_reason = "dash"
        return draft
    words = [w for w in re.split(r"\s+", body.strip()) if w]
    if len(words) > MAX_BODY_WORDS:
        draft.valid = False
        draft.reject_reason = "too_long"
        return draft
    if not body.strip().endswith("Josh Osborn"):
        draft.valid = False
        draft.reject_reason = "no_signature"
        return draft
    if MEETING_GUARANTEE not in body:
        draft.valid = False
        draft.reject_reason = "no_guarantee"
        return draft
    if "{" in body or "{" in subject:
        draft.valid = False
        draft.reject_reason = "placeholder"
        return draft
    first = _first_name(str((row or {}).get("name") or ""))
    if first.lower() == "there":
        draft.valid = False
        draft.reject_reason = "no_identity"
        return draft
    allowed = _numbers_in(str((row or {}).get("last_touch_snippet") or ""))
    allowed |= _numbers_in(GENERAL_PROOF)
    allowed |= _numbers_in(" ".join(CASE_STUDIES.values()))
    opener = body.split("\n", 1)[0]
    extra_nums = _numbers_in(opener) - allowed
    if extra_nums:
        draft.valid = False
        draft.reject_reason = "invented_number"
        return draft
    draft.valid = True
    draft.reject_reason = ""
    return draft


def nurture_row_from_candidate(c: TickerCandidate, now: datetime | None = None) -> dict:
    now = _aware(now or now_utc())
    signal = parse_signal_at(c.last_signal)
    if not signal:
        raise ValueError("no_signal_date")
    extra = c.extra or {}
    industry, basis = infer_industry_resolved(
        email=c.email,
        company=c.company,
        campaign=str(extra.get("campaign") or ""),
        website_text=str(extra.get("website_text") or ""),
        hs_industry=str(extra.get("hs_industry") or extra.get("industry") or ""),
    )
    fire = next_fire_at_from_signal(signal, now)
    return {
        "id": str(uuid4()),
        "name": c.name or "",
        "email": c.email or None,
        "phone": c.phone or None,
        "company": c.company or None,
        "hs_contact_id": c.hs_contact_id or None,
        "hs_deal_id": c.hs_deal_id or None,
        "status": "active",
        "source": c.source or None,
        "source_ref": extra.get("source_ref") or None,
        "signal_at": signal.isoformat(),
        "campaign": extra.get("campaign") or None,
        "campaign_id": str(extra.get("campaign_id") or "") or None,
        "industry": industry,
        "industry_basis": basis,
        "last_touch_snippet": scoped_snippet(str(extra.get("last_touch_snippet") or ""), extra | {"name": c.name, "email": c.email}),
        "nurture_thread_id": extra.get(NURTURE_THREAD_PROP) or None,
        "nurture_thread_subject": extra.get(NURTURE_SUBJECT_PROP) or None,
        "gmail_thread_id": extra.get(NURTURE_THREAD_PROP) or None,
        "original_subject": extra.get(NURTURE_SUBJECT_PROP) or None,
        "thread_kind": "reply" if extra.get(NURTURE_THREAD_PROP) else "new_thread",
        "in_reply_to": extra.get("in_reply_to") or None,
        "references": extra.get("references") or None,
        "reason": infer_nurture_reason(
            reason=c.reason,
            deal_stage=str(extra.get("deal_stage") or ""),
            extra=extra,
            booked=bool(extra.get("booked")),
            met=bool(extra.get("met")),
        ),
        "deal_stage": extra.get("deal_stage") or None,
        "met": bool(extra.get("met")),
        "booked": bool(extra.get("booked")),
        "meeting_at": extra.get("meeting_at") or None,
        "next_fire_at": fire.isoformat(),
        "nurture_state": "queued",
    }


def qualify_candidate(c: TickerCandidate) -> str:
    """Empty = enrollable. Else a skip reason."""
    blocked = is_not_deal_candidate(
        name=c.name,
        email=c.email,
        company=c.company,
        campaign=str((c.extra or {}).get("campaign") or ""),
        phone=c.phone,
        extra=c.extra or {},
    )
    if blocked:
        return "client_campaign" if blocked == "client" and (c.extra or {}).get("client_campaign") else blocked
    if (c.extra or {}).get("client_campaign"):
        return "client_campaign"
    if not parse_signal_at(c.last_signal):
        return "no_signal_date"
    if gmail_meeting_evidence(c.extra or {}).get("future_booking"):
        return "future_booking"
    if not has_meeting_qualification(
        source=c.source,
        reason=c.reason,
        extra=c.extra,
        deal_stage=str((c.extra or {}).get("deal_stage") or ""),
        booked=bool((c.extra or {}).get("booked")),
        met=bool((c.extra or {}).get("met")),
    ):
        return "reply_only"
    return ""


def apply_reenrollment(existing: list[dict], candidate: TickerCandidate, now: datetime | None = None) -> dict:
    """Active refresh / soft re-enroll / hard refuse. Returns a decision dict."""
    now = _aware(now or now_utc())
    email = (candidate.email or "").strip().lower()
    matches = [
        r
        for r in existing
        if email and (r.get("email") or "").strip().lower() == email
    ]
    signal = parse_signal_at(candidate.last_signal)
    if not signal:
        return {"action": "skip", "skip_reason": "no_signal_date"}
    active = [r for r in matches if (r.get("status") or "active") == "active"]
    if active:
        row = active[0]
        prev = parse_signal_at(row.get("signal_at"))
        if prev and signal <= prev:
            return {"action": "keep", "row": row}
        row["signal_at"] = signal.isoformat()
        row["last_touch_snippet"] = snippet_of(str((candidate.extra or {}).get("last_touch_snippet") or ""))
        if candidate.source:
            row["source"] = candidate.source
        if (candidate.extra or {}).get("campaign"):
            row["campaign"] = candidate.extra["campaign"]
        row["next_fire_at"] = (signal + timedelta(days=TICKER_DAYS)).isoformat()
        return {"action": "refresh", "row": row}
    stopped = [r for r in matches if (r.get("status") or "") == "stopped"]
    for row in stopped:
        reason = str(row.get("stop_reason") or "")
        if reason in HARD_STOPS or not reason:
            return {"action": "skip", "skip_reason": "hard_stopped", "row": row}
        if reason in SOFT_STOPS:
            new_row = nurture_row_from_candidate(candidate, now)
            return {"action": "new_active", "row": new_row}
    new_row = nurture_row_from_candidate(candidate, now)
    return {"action": "new_active", "row": new_row}


def build_nurture_card(row: dict, draft: NurtureDraft) -> dict[str, Any]:
    signal = parse_signal_at(row.get("signal_at"))
    signal_line = signal.astimezone(CDT).strftime("%b %-d, %Y") if signal else "unknown"
    snippet = scoped_snippet(str(row.get("last_touch_snippet") or ""), row)[:120]
    source = row.get("source") or "unknown"
    campaign = row.get("campaign") or ""
    ticker_id = str(row.get("id") or "")
    thread_kind = str(row.get("thread_kind") or ("reply" if row.get("gmail_thread_id") else "new_thread"))
    thread_label = "new thread" if thread_kind == "new_thread" else "thread reply"
    why = infer_nurture_reason(
        reason=str(row.get("reason") or ""),
        deal_stage=str(row.get("deal_stage") or ""),
        extra=row.get("extra") if isinstance(row.get("extra"), dict) else row,
        booked=bool((row.get("extra") or {}).get("booked") if isinstance(row.get("extra"), dict) else row.get("booked")),
        met=bool((row.get("extra") or {}).get("met") if isinstance(row.get("extra"), dict) else row.get("met")),
    )
    source_id = draft.spoken_source_id or draft.meeting_source_id or meeting_source_id(row)
    source_id_line = f"Meeting source: {source_id}\n" if source_id else ""
    fallback = (
        f"90-day ticker (approve before send)\n"
        f"To: {row.get('email') or row.get('phone')}\n"
        f"Why: {why}\n"
        f"Thread: {thread_label}\n"
        f"Source: {source} / {campaign}\n"
        f"{source_id_line}"
        f"Signal: {signal_line}\n"
        f'They said: "{snippet}"\n'
        f"Subject: {draft.subject}\n\n{draft.body}"
    )
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "90-day ticker (approve before send)", "emoji": True},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*To:* {row.get('email') or row.get('phone') or row.get('name')}\n"
                    f"*Why:* {why}\n"
                    f"*Thread:* {thread_label}\n"
                    f"*Source:* {source} / {campaign or '-'}\n"
                    + (f"*Meeting source:* {source_id}\n" if source_id else "")
                    + f"*Signal:* {signal_line}\n"
                    f'*They said:* "{snippet}"'
                ),
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Subject:* {draft.subject}\n\n```{draft.body}```",
            },
        },
        {
            "type": "actions",
            "block_id": f"nurture_actions_{ticker_id}",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Approve & send", "emoji": True},
                    "style": "primary",
                    "action_id": ACTION_APPROVE,
                    "value": ticker_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Edit & send", "emoji": True},
                    "action_id": ACTION_EDIT,
                    "value": ticker_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Remove from nurture", "emoji": True},
                    "style": "danger",
                    "action_id": ACTION_REMOVE,
                    "value": ticker_id,
                },
            ],
        },
    ]
    return {
        "text": fallback,
        "blocks": blocks,
        "subject": draft.subject,
        "body": draft.body,
        "source_id": source_id,
        "spoken_source_id": draft.spoken_source_id,
        "meeting_source_id": draft.meeting_source_id,
    }


def outcome_blocks(row: dict, outcome: str, detail: str = "") -> list[dict]:
    label = {
        "sent": "Sent from Josh's Gmail as a thread reply or new 1:1. Removed from nurture for 90 days.",
        "removed": "Removed from nurture permanently. Ticker row stopped.",
        "already_sent": "Already sent. Buttons are locked (idempotent).",
        "already_removed": "Already removed. Buttons are locked.",
        "disabled": "NURTURE_SEND_ENABLED is off. Nothing was sent.",
        "error": detail or "Send failed.",
    }.get(outcome, detail or outcome)
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Nurture:* {row.get('name') or row.get('email')}\n"
                    f"*Outcome:* {label}"
                ),
            },
        }
    ]


def edit_modal(row: dict, draft: NurtureDraft, channel: str = "", ts: str = "") -> dict[str, Any]:
    return {
        "type": "modal",
        "callback_id": VIEW_EDIT,
        "private_metadata": json.dumps({"ticker_id": row.get("id"), "channel": channel, "ts": ts}),
        "title": {"type": "plain_text", "text": "Edit & send"},
        "submit": {"type": "plain_text", "text": "Send"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": "nurture_subject",
                "element": {
                    "type": "plain_text_input",
                    "action_id": "subject",
                    "initial_value": draft.subject,
                },
                "label": {"type": "plain_text", "text": "Subject"},
            },
            {
                "type": "input",
                "block_id": "nurture_body",
                "element": {
                    "type": "plain_text_input",
                    "action_id": "body",
                    "multiline": True,
                    "initial_value": draft.body,
                },
                "label": {"type": "plain_text", "text": "Body"},
            },
        ],
    }


def verify_slack_signature(secret: str, timestamp: str, body: bytes | str, signature: str) -> bool:
    if not secret or not timestamp or not signature:
        return False
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs(int(now_utc().timestamp()) - ts) > 60 * 5:
        return False
    raw = body if isinstance(body, (bytes, bytearray)) else str(body).encode("utf-8")
    basestring = b"v0:" + str(ts).encode("ascii") + b":" + raw
    digest = "v0=" + hmac.new(secret.encode("utf-8"), basestring, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, signature)


def thread_reply_headers(subject: str, in_reply_to: str = "", references: str = "") -> dict[str, str]:
    sub = (subject or "").strip()
    if sub and not sub.lower().startswith("re:"):
        sub = f"Re: {sub}"
    refs = references or in_reply_to
    headers = {"Subject": sub}
    if in_reply_to:
        headers["In-Reply-To"] = in_reply_to
    if refs:
        headers["References"] = refs
    return headers


def cooldown_until(sent_at: datetime | None = None) -> datetime:
    return (_aware(sent_at) or now_utc()) + timedelta(days=TICKER_DAYS)


def fire_gate(
    row: dict,
    *,
    settings: Settings | None = None,
    hs=None,
    gmail=None,
    smartlead_sent: list | None = None,
    gmail_sent_at: datetime | None = None,
    future_meetings: bool = False,
    deal_404: bool = False,
    associated_stages: list | None = None,
    last_activity=None,
    now: datetime | None = None,
) -> tuple[str, dict]:
    """G5, G2, G6, G3, G4, G1. Returns (skip_reason, patch)."""
    now = _aware(now or now_utc())
    name = str(row.get("name") or "").strip()
    email = str(row.get("email") or "").strip()
    if not name and not email:
        patch = {"status": "stopped", "stop_reason": "no_identity", "stopped_at": now.isoformat()}
        return "no_identity", patch
    blocked = is_not_deal_candidate(
        name=name,
        email=email,
        company=str(row.get("company") or ""),
        campaign=str(row.get("campaign") or ""),
        phone=str(row.get("phone") or ""),
        extra=row.get("extra") if isinstance(row.get("extra"), dict) else row,
        contact=row.get("contact") if isinstance(row.get("contact"), dict) else None,
        deals=row.get("deals") if isinstance(row.get("deals"), list) else None,
    )
    if blocked:
        patch = {"status": "stopped", "stop_reason": blocked, "stopped_at": now.isoformat()}
        return blocked, patch
    stages = list(associated_stages or row.get("associated_stages") or [])
    if row.get("deal_stage"):
        stages.append(str(row.get("deal_stage")))
    canon_stages = {canonicalize_stage(s) or str(s) for s in stages}
    if STAGE["closed_won"] in canon_stages:
        patch = {"status": "stopped", "stop_reason": "client", "stopped_at": now.isoformat()}
        return "client", patch
    if STAGE["contract_signed_unpaid"] in canon_stages or STAGE["poc"] in canon_stages:
        patch = {"status": "stopped", "stop_reason": "booked", "stopped_at": now.isoformat()}
        return "booked", patch
    if row.get("unsubscribed") or (row.get("extra") or {}).get("unsubscribed"):
        patch = {"status": "stopped", "stop_reason": "unsubscribed", "stopped_at": now.isoformat()}
        return "unsubscribed", patch
    if deal_404 and row.get("hs_deal_id"):
        patch = {"status": "stopped", "stop_reason": "deal_archived", "stopped_at": now.isoformat()}
        return "deal_archived", patch
    if future_meetings or STAGE["discovery_scheduled"] in stages:
        patch = {"status": "stopped", "stop_reason": "booked", "stopped_at": now.isoformat()}
        return "booked", patch
    activity = parse_signal_at(last_activity or row.get("last_activity"))
    if STAGE["discovery_completed"] in stages or STAGE["proposal_sent"] in stages:
        if activity and now - activity < timedelta(days=STALLED_DAYS):
            patch = {"status": "stopped", "stop_reason": "booked", "stopped_at": now.isoformat()}
            return "booked", patch
        if not activity and row.get("open_booked"):
            patch = {"status": "stopped", "stop_reason": "booked", "stopped_at": now.isoformat()}
            return "booked", patch
    if gmail_sent_at:
        nxt = gmail_sent_at + timedelta(days=TICKER_DAYS)
        patch = {"status": "active", "stop_reason": "emailed_recently", "next_fire_at": nxt.isoformat()}
        return "emailed_recently", patch
    if smartlead_sent:
        latest = max(parse_signal_at(s.get("time") if isinstance(s, dict) else s) or now for s in smartlead_sent)
        if now - latest <= timedelta(days=EMAILED_RECENTLY_DAYS):
            nxt = latest + timedelta(days=TICKER_DAYS)
            patch = {"status": "active", "stop_reason": "emailed_recently", "next_fire_at": nxt.isoformat()}
            return "emailed_recently", patch
    if settings and event_predates_freeze(
        Engagement(source="nurture", external_id=str(row.get("id") or ""), occurred_at=parse_signal_at(row.get("signal_at"))),
        settings,
    ):
        return "manual_freeze", {}
    return "", {}


def select_due_with_cap(
    rows: list[dict],
    *,
    now: datetime,
    max_per_weekday: int = NURTURE_MAX_PER_WEEKDAY,
) -> tuple[list[dict], list[dict]]:
    due = [
        r
        for r in rows
        if (r.get("status") or "") == "active"
        and str(r.get("next_fire_at") or "") <= now.isoformat()
    ]
    due.sort(key=lambda r: parse_signal_at(r.get("signal_at") or r.get("next_fire_at")) or now)
    today_key = now.astimezone(CDT).date().isoformat()
    posted = due[:max_per_weekday]
    rolled = []
    nxt = next_weekday(now.astimezone(CDT) + timedelta(days=1))
    for row in due[max_per_weekday:]:
        copy = dict(row)
        copy["next_fire_at"] = datetime(nxt.year, nxt.month, nxt.day, tzinfo=CDT).isoformat()
        rolled.append(copy)
    del today_key
    return posted, rolled


def fire_due_rows(
    settings: Settings,
    memory: Memory,
    report: CycleReport,
    *,
    now: datetime | None = None,
    slack=None,
    gmail=None,
) -> list[dict]:
    """Evaluate due ticker rows. Post Block Kit only when NURTURE_POST_ENABLED."""
    now = _aware(now or now_utc())
    if getattr(settings, "supabase_url", "") and getattr(settings, "supabase_key", "") and memory.use_supabase:
        if any("ticker_supabase_unavailable" in e or "due_ticker" in e for e in memory.errors):
            if "ticker_supabase_unavailable" not in report.errors:
                report.errors.append("ticker_supabase_unavailable")
            return []
    try:
        due = memory.due_ticker(now.isoformat())
    except Exception as exc:
        report.errors.append(f"ticker_supabase_unavailable: {exc}")
        return []
    if memory.use_supabase and any("due_ticker" in e for e in memory.errors):
        if "ticker_supabase_unavailable" not in report.errors:
            report.errors.append("ticker_supabase_unavailable")
        return []
    cards: list[dict] = []
    gmail = gmail_client_for_cards(settings, gmail)
    posted, rolled = select_due_with_cap(due, now=now)
    for row in rolled:
        memory.bump_ticker(str(row.get("id") or row.get("email")), row["next_fire_at"], now.isoformat())
        report.ticker_skipped.append(f"{row.get('email') or row.get('name')} rolled")
    post_on = bool(getattr(settings, "nurture_post_enabled", False))
    for row in posted:
        if deal_is_locked({"properties": {"crmbrain_locked": row.get("crmbrain_locked")}}):
            report.ticker_skipped.append(f"{row.get('email') or row.get('name')} locked")
            continue
        reason, patch = fire_gate(row, settings=settings, now=now)
        if reason:
            if patch:
                if patch.get("status") == "stopped":
                    memory.stop_ticker(
                        email=row.get("email"),
                        hs_contact_id=row.get("hs_contact_id"),
                        stop_reason=patch.get("stop_reason"),
                    )
                elif patch.get("next_fire_at"):
                    memory.bump_ticker(str(row.get("id") or row.get("email")), patch["next_fire_at"], now.isoformat())
            report.ticker_skipped.append(f"{row.get('email') or row.get('name')} {reason}")
            continue
        row = attach_gmail_thread(row, gmail)
        row = apply_gmail_meeting_evidence(row, gmail)
        if row.get("skip_nurture") == "future_booking":
            report.ticker_skipped.append(f"{row.get('email') or row.get('name')} future_booking")
            continue
        row["reason"] = infer_nurture_reason(
            reason=str(row.get("reason") or ""),
            deal_stage=str(row.get("deal_stage") or ""),
            extra=row.get("extra") if isinstance(row.get("extra"), dict) else row,
            booked=bool(row.get("booked") or (row.get("extra") or {}).get("booked") if isinstance(row.get("extra"), dict) else row.get("booked")),
            met=bool(row.get("met") or (row.get("extra") or {}).get("met") if isinstance(row.get("extra"), dict) else row.get("met")),
        )
        row["last_touch_snippet"] = scoped_snippet(str(row.get("last_touch_snippet") or ""), row)
        draft = compose_nurture_draft(row)
        if not draft.valid:
            report.review_queue.append(f"{row.get('name')} G7 {draft.reject_reason}")
            continue
        card = build_nurture_card(row, draft)
        cards.append({**card, "ticker_id": row.get("id"), "email": row.get("email"), "name": row.get("name")})
        report.ticker_drafts.append(row.get("email") or row.get("name") or row.get("id"))
        report.nurture_cards.append(card)
        if post_on:
            try:
                from crmbrain import slack_notify

                posted_msg = (slack or slack_notify).post_blocks(settings, card["text"], card["blocks"])
                if posted_msg and row.get("id"):
                    memory.patch_ticker(
                        str(row["id"]),
                        {
                            "slack_channel": posted_msg.get("channel") or settings.slack_channel,
                            "slack_ts": posted_msg.get("ts") or "",
                            "draft_subject": draft.subject,
                            "draft_body": draft.body,
                            "nurture_state": "queued",
                        },
                    )
            except Exception as exc:
                report.errors.append(f"slack ticker: {exc}")
            next_fire = (now + timedelta(days=TICKER_DAYS)).isoformat()
            memory.bump_ticker(str(row.get("id") or row.get("email")), next_fire, now.isoformat())
    return cards


def dry_run_report(candidates_by_source: dict[str, int], cards: list[dict], skips: dict[str, int], schedule: dict) -> dict:
    return {
        "by_source": candidates_by_source,
        "by_skip_reason": skips,
        "industry_hit_rate": {},
        "weekly_schedule": schedule,
        "sample_drafts": cards[:10],
        "writes": 0,
        "slack_posts": 0,
    }


INBOUND_TYPES = frozenset({"REPLY", "RECEIVED", "INBOUND"})


def is_positive_lead(lead: dict) -> bool:
    cid = lead.get("category_id")
    if cid in POSITIVE_CATEGORY_IDS:
        return True
    cat = str(lead.get("category") or lead.get("sentiment_type") or "").lower()
    if cat in {"interested", "meeting request", "information request", "positive", "positive reply"}:
        return True
    # Fixture cases T-02/T-03 already selected the lead; missing category still collects.
    return cid is None and not cat


def latest_inbound(history: list[dict]) -> dict | None:
    inbound: list[dict] = []
    for msg in history or []:
        kind = str(msg.get("type") or "").upper()
        if kind in INBOUND_TYPES:
            inbound.append(msg)
    if not inbound:
        return None
    inbound.sort(key=lambda m: parse_signal_at(m.get("time") or m.get("date")) or datetime.min.replace(tzinfo=timezone.utc))
    return inbound[-1]


def collect_s1_positives(campaigns: list[dict], leads: list[dict], histories: dict | None = None) -> list[TickerCandidate]:
    """S1: every campaign status. Positives only. Missing inbound → no_signal_date."""
    camp_by_id = {c.get("id"): c for c in campaigns or []}
    histories = histories or {}
    out: list[TickerCandidate] = []
    for lead in leads or []:
        if not is_positive_lead(lead):
            continue
        camp = camp_by_id.get(lead.get("campaign_id")) or {}
        key = f"{lead.get('campaign_id')}:{lead.get('lead_id')}"
        history = histories.get(key) or histories.get(lead.get("lead_id")) or lead.get("message_history") or []
        inbound = latest_inbound(history)
        extra = {
            "campaign": camp.get("name") or lead.get("campaign") or "",
            "campaign_id": camp.get("id") or lead.get("campaign_id"),
            "source_ref": f"smartlead:{camp.get('id') or lead.get('campaign_id')}:{lead.get('lead_id')}",
            "last_touch_snippet": scoped_snippet(
                (inbound or {}).get("email_body") or (inbound or {}).get("body") or "",
                {"name": f"{lead.get('first_name') or ''} {lead.get('last_name') or ''}".strip() or lead.get("name") or "", "email": lead.get("email") or ""},
            ),
            "client_campaign": bool(camp.get("client_campaign") or lead.get("client_campaign")),
            "booked": bool(lead.get("booked") or lead.get("met") or camp.get("booked")),
            "met": bool(lead.get("met")),
        }
        name = f"{lead.get('first_name') or ''} {lead.get('last_name') or ''}".strip() or lead.get("name") or ""
        c = TickerCandidate(
            name=name,
            email=str(lead.get("email") or ""),
            company=str(lead.get("company") or ""),
            reason=infer_nurture_reason(
                extra=extra,
                booked=bool(extra.get("booked")),
                met=bool(extra.get("met")),
            ),
            last_signal=parse_signal_at((inbound or {}).get("time") or (inbound or {}).get("date")) if inbound else None,
            source="smartlead",
            extra=extra,
        )
        if not inbound:
            c.skip_reason = "no_signal_date"
        out.append(c)
    return out


def collect_s2_hubspot(deals: list[dict], *, now: datetime | None = None) -> list[TickerCandidate]:
    """S2: Nurture deals + stalled DC/PS (30d+). signal_at from note/activity, never hs_lastmodifieddate."""
    now = _aware(now or now_utc())
    out: list[TickerCandidate] = []
    for deal in deals or []:
        stage = str(deal.get("dealstage") or deal.get("deal_stage") or "")
        contact = deal.get("contact") or {}
        note = deal.get("source_note") or {}
        last_activity = parse_signal_at(deal.get("last_activity"))
        note_at = parse_signal_at(note.get("created") or note.get("date"))
        is_nurture = stage == STAGE["nurture"]
        is_stalled_open = stage in {
            STAGE["discovery_held"],
            STAGE["discovery_completed"],
            STAGE["proposal_sent"],
        }
        pipeline = str(deal.get("pipeline") or (deal.get("properties") or {}).get("pipeline") or "")
        if pipeline == RENEWAL_PIPELINE:
            continue
        if is_archived_hs_row(deal) or is_archived_hs_row(contact if isinstance(contact, dict) else None):
            continue
        if is_stalled_open:
            if not last_activity or now - last_activity < timedelta(days=STALLED_DAYS):
                continue
        elif not is_nurture:
            continue
        if is_not_deal_candidate(
            name=f"{contact.get('firstname') or ''} {contact.get('lastname') or ''}".strip()
            or str(deal.get("contact_name") or ""),
            email=str(contact.get("email") or deal.get("contact_email") or ""),
            company=str(contact.get("company") or ""),
            phone=str(contact.get("phone") or ""),
            extra=deal.get("extra") if isinstance(deal.get("extra"), dict) else deal,
            contact=contact if contact else None,
            deals=[deal],
        ):
            continue
        signal = note_at or last_activity
        if not signal:
            continue
        contact_row = {
            "name": f"{contact.get('firstname') or ''} {contact.get('lastname') or ''}".strip() or deal.get("contact_name") or "",
            "email": contact.get("email") or deal.get("contact_email") or "",
            "dealname": deal.get("dealname") or (deal.get("properties") or {}).get("dealname") or "",
        }
        raw_note = str(note.get("body") or "")
        raw_snip = str(deal.get("snippet") or "")
        campaign = ""
        source_blob = raw_note or raw_snip
        if source_blob.lower().startswith("source:"):
            first = source_blob.split(".", 1)[0]
            campaign = first.split(":", 1)[-1].strip()
        if looks_like_deal_name(raw_snip, contact_row) or raw_snip.lower() == str(contact_row.get("dealname") or "").lower():
            raw_snip = ""
        snippet = scoped_snippet(raw_note or raw_snip, contact_row) or (
            snippet_of(raw_note) if raw_note.lower().startswith("source:") else ""
        )
        extra = {
            "source_ref": f"hubspot:deal:{deal.get('id')}:contact:{contact.get('id') or ''}",
            "last_touch_snippet": snippet,
            "campaign": campaign or deal.get("campaign") or "",
            "deal_stage": stage,
            "booked": True,
            "met": bool(
                deal.get("met")
                or deal.get("fireflies")
                or deal.get("cube_acr")
                or deal.get("meeting_at")
                or deal.get("hs_meeting")
                or deal.get("meeting_engagement")
                or stage
                in {
                    STAGE["discovery_completed"],
                    STAGE["proposal_sent"],
                    STAGE["nurture"],
                }
            ),
            "fireflies": deal.get("fireflies"),
            "cube_acr": deal.get("cube_acr") or deal.get("cube"),
            "meeting_at": deal.get("meeting_at"),
            "hs_meeting": deal.get("hs_meeting") or deal.get("meeting_engagement"),
            "no_show_count": (deal.get("properties") or {}).get("no_show_count")
            or deal.get("no_show_count"),
            "website_text": deal.get("website_text") or contact.get("website_text") or "",
            "hs_industry": (
                str(deal.get("company_industry") or "")
                or (
                    str((deal.get("company") or {}).get("industry") or "")
                    if isinstance(deal.get("company"), dict)
                    else ""
                )
                or str(contact.get("company_industry") or "")
                or str((contact.get("properties") or {}).get("industry") or "")
                or str(contact.get("industry") or "")
            ),
            "nurture_thread_id": contact.get(NURTURE_THREAD_PROP)
            or (contact.get("properties") or {}).get(NURTURE_THREAD_PROP)
            or deal.get(NURTURE_THREAD_PROP)
            or (deal.get("properties") or {}).get(NURTURE_THREAD_PROP)
            or "",
            "nurture_thread_subject": contact.get(NURTURE_SUBJECT_PROP)
            or (contact.get("properties") or {}).get(NURTURE_SUBJECT_PROP)
            or deal.get(NURTURE_SUBJECT_PROP)
            or (deal.get("properties") or {}).get(NURTURE_SUBJECT_PROP)
            or "",
        }
        name = contact_row["name"]
        out.append(
            TickerCandidate(
                name=name,
                email=str(contact.get("email") or deal.get("contact_email") or ""),
                company=str(contact.get("company") or ""),
                reason=infer_nurture_reason(
                    reason="kicked_can" if is_nurture else "",
                    deal_stage=stage,
                    extra=extra,
                    booked=True,
                    met=bool(extra.get("met")),
                ),
                last_signal=signal,
                hs_contact_id=str(contact.get("id") or ""),
                hs_deal_id=str(deal.get("id") or ""),
                source="hubspot",
                extra=extra,
            )
        )
    return out


def collect_s3_gmail(threads: list[dict]) -> list[TickerCandidate]:
    """S3: positive inbound Gmail. Collection only; Josh enroll still requires met/booked."""
    out: list[TickerCandidate] = []
    for thread in threads or []:
        messages = thread.get("messages") or []
        inbound = None
        for msg in messages:
            frm = str(msg.get("from") or "").lower()
            if "salesglider" in frm or "joshua@" in frm:
                continue
            inbound = msg
        if not inbound:
            continue
        contact_row = {
            "name": str(thread.get("name") or inbound.get("name") or ""),
            "email": str(thread.get("email") or inbound.get("from") or ""),
        }
        extra = {
            "source_ref": f"gmail:{thread.get('thread_id')}",
            "last_touch_snippet": scoped_snippet(str(inbound.get("body") or ""), contact_row),
            "original_subject": "",
            "thread_kind": "new_thread",
            "in_reply_to": "",
            "references": "",
            "booked": bool(thread.get("booked") or thread.get("met")),
            "met": bool(thread.get("met") or thread.get("fireflies") or thread.get("cube_acr")),
            "fireflies": thread.get("fireflies"),
            "cube_acr": thread.get("cube_acr"),
        }
        out.append(
            TickerCandidate(
                name=contact_row["name"],
                email=contact_row["email"],
                reason=infer_nurture_reason(
                    reason="kicked_can" if thread.get("intent") == "timing_later" else "",
                    extra=extra,
                    booked=bool(extra.get("booked")),
                    met=bool(extra.get("met")),
                ),
                last_signal=parse_signal_at(inbound.get("date")),
                source="gmail",
                extra=extra,
            )
        )
    return out


def sample_card_rows() -> list[dict]:
    """Ten dry-run rows: mix of industry, general, HubSpot Nurture, and Gmail."""
    now = datetime(2026, 10, 2, 23, 0, tzinfo=timezone.utc)
    rows = [
        {
            "id": "sample-jackie",
            "name": "Jackie Darkazalli",
            "email": "jackie@kellyroofing.com",
            "company": "Kelly Roofing",
            "reason": "kicked_can",
            "source": "hubspot",
            "campaign": "",
            "industry": "roofing",
            "industry_basis": "domain",
            "signal_at": "2026-07-01T16:00:00+00:00",
            "last_touch_snippet": "Check back after our busy season. Roofing crews are slammed until fall.",
            "gmail_thread_id": "thread-jackie",
            "original_subject": "Kelly Roofing intro",
            "thread_kind": "reply",
            "in_reply_to": "<jackie-orig@mail>",
        },
        {
            "id": "sample-joel",
            "name": "Joel Stewart",
            "email": "joel@thechillbrothers.com",
            "company": "The Chill Brothers",
            "reason": "kicked_can",
            "source": "hubspot",
            "campaign": "",
            "industry": "hvac",
            "industry_basis": "website",
            "website_text": "The Chill Brothers | HVAC, heating and cooling repair",
            "signal_at": "2026-07-10T16:00:00+00:00",
            "last_touch_snippet": "We are slammed through summer, check back in the fall about filling shoulder season.",
            "gmail_thread_id": "thread-joel",
            "original_subject": "HVAC shoulder season",
            "thread_kind": "reply",
        },
        {
            "id": "sample-dana",
            "name": "Dana Ortiz",
            "email": "dana@brightcool.test",
            "company": "BrightCool Heating & Air",
            "reason": "kicked_can",
            "source": "hubspot",
            "campaign": "SalesGlider HVAC Sports Offer",
            "industry": "hvac",
            "industry_basis": "campaign",
            "signal_at": "2026-07-10T16:00:00+00:00",
            "last_touch_snippet": "Dana said they are slammed through summer, check back in the fall.",
            "thread_kind": "new_thread",
        },
        {
            "id": "sample-pat",
            "name": "Pat Reyes",
            "email": "pat@summitroofs.test",
            "company": "Summit Roofs",
            "reason": "booked",
            "source": "smartlead",
            "campaign": "SalesGlider Roofers",
            "industry": "roofing",
            "industry_basis": "campaign",
            "signal_at": "2026-06-03T17:20:00+00:00",
            "last_touch_snippet": "Yes, send me info. Spring is our slow season.",
            "thread_kind": "new_thread",
            "extra": {"booked": True},
        },
        {
            "id": "sample-casey",
            "name": "Casey Lin",
            "email": "casey@linholdings.test",
            "company": "Lin Holdings",
            "reason": "kicked_can",
            "source": "hubspot",
            "campaign": "",
            "industry": None,
            "signal_at": "2026-08-01T12:00:00+00:00",
            "last_touch_snippet": "Maybe later this year.",
            "thread_kind": "new_thread",
        },
        {
            "id": "sample-morgan",
            "name": "Morgan Pike",
            "email": "morgan@peakplumb.test",
            "company": "Peak Plumbing",
            "reason": "kicked_can",
            "source": "gmail",
            "campaign": "",
            "industry": None,
            "signal_at": "2026-06-21T18:30:00+00:00",
            "last_touch_snippet": "Interested but timing is bad until Q4. Ping me then.",
            "gmail_thread_id": "t-777",
            "original_subject": "Q4 follow up",
            "thread_kind": "reply",
            "in_reply_to": "<morgan-q4@mail>",
            "references": "<morgan-q4@mail>",
        },
        {
            "id": "sample-lee",
            "name": "Lee Ng",
            "email": "lee@bytewise.test",
            "company": "Bytewise",
            "reason": "booked",
            "source": "smartlead",
            "campaign": "SalesGlider MSPs",
            "industry": "msp",
            "industry_basis": "campaign",
            "signal_at": "2026-06-15T12:00:00+00:00",
            "last_touch_snippet": "We just lost our SDR, maybe now.",
            "thread_kind": "new_thread",
            "extra": {"booked": True},
        },
        {
            "id": "sample-ari",
            "name": "Ari Stone",
            "email": "stalled@fastpipe.test",
            "company": "FastPipe",
            "reason": "kicked_can",
            "source": "hubspot",
            "campaign": "",
            "industry": None,
            "deal_stage": STAGE["proposal_sent"],
            "signal_at": "2026-08-18T15:00:00+00:00",
            "last_touch_snippet": "Proposal is sitting. No activity since mid-August.",
            "thread_kind": "new_thread",
            "met": True,
        },
        {
            "id": "sample-earl",
            "name": "Earl Jackson",
            "email": "ej@accg-inc.com",
            "company": "Accg Inc - Concrete Contractor Orlando",
            "reason": "kicked_can",
            "source": "hubspot",
            "campaign": "",
            "industry": "construction",
            "industry_basis": "website",
            "signal_at": "2026-07-20T15:00:00+00:00",
            "last_touch_snippet": "Concrete season is busy. Let's talk after this pour.",
            "thread_kind": "new_thread",
        },
        {
            "id": "sample-sam",
            "name": "Sam Cole",
            "email": "sam@hireright.test",
            "company": "HireRight",
            "reason": "met",
            "source": "hubspot",
            "campaign": "SalesGlider Staffing",
            "industry": "staffing",
            "industry_basis": "campaign",
            "signal_at": "2026-06-10T12:00:00+00:00",
            "last_touch_snippet": "Need to fill two AE seats before Q4.",
            "thread_kind": "new_thread",
            "deal_stage": STAGE["discovery_completed"],
        },
    ]
    del now
    return rows


def render_sample_cards(rows: list[dict] | None = None) -> list[dict]:
    cards = []
    for row in rows or sample_card_rows():
        draft = compose_nurture_draft(row)
        card = build_nurture_card(row, draft)
        attached = attach_gmail_thread(row, None)
        cards.append(
            {
                "name": row.get("name"),
                "email": row.get("email"),
                "source": row.get("source"),
                "industry": row.get("industry"),
                "reason": infer_nurture_reason(
                    reason=str(row.get("reason") or ""),
                    deal_stage=str(row.get("deal_stage") or ""),
                    extra=row.get("extra") if isinstance(row.get("extra"), dict) else row,
                    booked=bool((row.get("extra") or {}).get("booked") if isinstance(row.get("extra"), dict) else row.get("booked")),
                    met=bool((row.get("extra") or {}).get("met") if isinstance(row.get("extra"), dict) else row.get("met")),
                ),
                "thread_id": attached.get("gmail_thread_id")
                or attached.get("thread_id")
                or row.get("gmail_thread_id")
                or row.get("thread_id")
                or "",
                "thread_kind": attached.get("thread_kind") or row.get("thread_kind") or "new_thread",
                "subject": draft.subject,
                "body": draft.body,
                "valid": draft.valid,
                "blocks": card["blocks"],
                "fallback_text": card["text"],
                "source_id": draft.spoken_source_id or draft.meeting_source_id,
                "spoken_source_id": draft.spoken_source_id,
                "meeting_source_id": draft.meeting_source_id,
            }
        )
    return cards


_SAMPLE_DEAL_PROPS = [
    "dealname",
    "dealstage",
    "pipeline",
    "createdate",
    "closedate",
    "notes_last_contacted",
    "notes_last_updated",
    "hs_last_sales_activity_timestamp",
    "nurture_reason",
    "nurture_thread_id",
    "nurture_thread_subject",
    "no_show_count",
    "engagements_last_meeting_booked",
    "description",
]


def _contact_fields(contact: dict | None) -> dict[str, str]:
    props = (contact or {}).get("properties") or {}
    first = str(props.get("firstname") or "").strip()
    last = str(props.get("lastname") or "").strip()
    return {
        "name": f"{first} {last}".strip(),
        "email": str(props.get("email") or "").strip(),
        "phone": str(props.get("phone") or "").strip(),
        "company": str(props.get("company") or "").strip(),
        "industry": str(props.get("industry") or "").strip(),
        "nurture_thread_id": str(props.get(NURTURE_THREAD_PROP) or "").strip(),
        "nurture_thread_subject": str(props.get(NURTURE_SUBJECT_PROP) or "").strip(),
        "snippet": " ".join(
            str(props.get(k) or "")
            for k in ("personal_details", "pain_points", "relationship_hooks")
            if props.get(k)
        )[:SNIPPET_MAX],
    }


def _harvest_block_sets(hs) -> tuple[set[str], set[str], set[str]]:
    """Closed Won emails/domains and Client Renewals emails from HubSpot."""
    from crmbrain.config import FREE_MAIL_DOMAINS, JOSH_DOMAINS, email_domain

    won_emails: set[str] = set()
    won_domains: set[str] = set()
    renewal_emails: set[str] = set()
    search = getattr(hs, "search_objects", None)
    contacts_for = getattr(hs, "contacts_for_deal", None)
    if not callable(search) or not callable(contacts_for):
        return won_emails, won_domains, renewal_emails
    try:
        won_deals = search(
            "deals",
            [{"propertyName": "dealstage", "operator": "EQ", "value": STAGE["closed_won"]}],
            _SAMPLE_DEAL_PROPS,
            max_results=400,
        )
    except Exception:
        won_deals = []
    for deal in won_deals or []:
        if is_archived_hs_row(deal):
            continue
        try:
            contacts = contacts_for(str(deal.get("id") or ""))
        except Exception:
            contacts = []
        for contact in contacts or []:
            if is_archived_hs_row(contact):
                continue
            fields = _contact_fields(contact)
            email = fields["email"].lower()
            if email:
                won_emails.add(email)
                host = email_domain(email)
                if host and host not in FREE_MAIL_DOMAINS and host not in JOSH_DOMAINS:
                    won_domains.add(host)
    try:
        renewal_deals = search(
            "deals",
            [{"propertyName": "pipeline", "operator": "EQ", "value": RENEWAL_PIPELINE}],
            _SAMPLE_DEAL_PROPS,
            max_results=400,
        )
    except Exception:
        renewal_deals = []
    for deal in renewal_deals or []:
        if is_archived_hs_row(deal):
            continue
        try:
            contacts = contacts_for(str(deal.get("id") or ""))
        except Exception:
            contacts = []
        for contact in contacts or []:
            if is_archived_hs_row(contact):
                continue
            email = _contact_fields(contact)["email"].lower()
            if email:
                renewal_emails.add(email)
    return won_emails, won_domains, renewal_emails


def _require_gmail(settings: Settings):
    """Construct the live Gmail client. Import and config errors are not swallowed."""
    from crmbrain.gmail_client import Gmail

    if not (
        getattr(settings, "gmail_refresh_token", "")
        and getattr(settings, "gmail_client_id", "")
        and getattr(settings, "gmail_client_secret", "")
    ):
        raise RuntimeError("missing_gmail_config")
    return Gmail(settings)


def company_cycle_key(company: str = "", email: str = "", dealname: str = "") -> str:
    from crmbrain.config import FREE_MAIL_DOMAINS, JOSH_DOMAINS, email_domain

    label = display_company_name(company, dealname, email)
    if label:
        return label.lower()
    host = email_domain(email)
    if host and host not in FREE_MAIL_DOMAINS and host not in JOSH_DOMAINS:
        return f"domain:{host}"
    return ""


def _card_rank(row: dict) -> tuple:
    met = 1 if str(row.get("reason") or "") == "met" or row.get("met") else 0
    thread = 1 if stored_nurture_thread_id(row) else 0
    when = parse_signal_at(row.get("meeting_at") or row.get("signal_at")) or datetime.min.replace(
        tzinfo=timezone.utc
    )
    return (met, thread, when)


def _pick_one_per_company(rows: list[dict]) -> tuple[list[dict], int]:
    groups: dict[str, list[dict]] = {}
    unique: list[dict] = []
    for row in rows:
        key = company_cycle_key(
            str(row.get("company") or ""),
            str(row.get("email") or ""),
            str(row.get("dealname") or ""),
        )
        if not key:
            unique.append(row)
            continue
        groups.setdefault(key, []).append(row)
    dropped = 0
    for members in groups.values():
        members.sort(key=_card_rank, reverse=True)
        unique.append(members[0])
        dropped += max(0, len(members) - 1)
    return unique, dropped


def sample_hubspot_nurture_cards(
    settings: Settings,
    limit: int = 10,
    *,
    hs=None,
    gmail=None,
    out_path: str | None = None,
) -> dict[str, Any]:
    """Read HubSpot Nurture-stage deals and compose sample cards.

    Cards come from dealstage 3486952153 only. Hard-excluded people, archived
    rows, Closed Won clients/domains, and Client Renewals contacts are dropped.
    One card per company per run.
    """
    from pathlib import Path

    from crmbrain.config import email_domain

    limit = max(1, int(limit or 10))
    gmail = gmail_client_for_cards(settings, gmail)
    if hs is None:
        if not getattr(settings, "hubspot_token", ""):
            raise RuntimeError("missing_hubspot_token")
        from crmbrain.hubspot import HubSpot

        hs = HubSpot(settings)
    won_emails, won_domains, renewal_emails = _harvest_block_sets(hs)
    search = hs.search_objects
    nurture_deals = search(
        "deals",
        [
            {"propertyName": "dealstage", "operator": "EQ", "value": STAGE["nurture"]},
        ],
        _SAMPLE_DEAL_PROPS,
        max_results=400,
    )
    skipped: dict[str, int] = {}
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    last_meeting = getattr(hs, "last_meeting_at", None)
    for deal in nurture_deals or []:
        props = deal.get("properties") or {}
        pipeline = str(props.get("pipeline") or deal.get("pipeline") or "")
        if pipeline == RENEWAL_PIPELINE:
            skipped["renewal_pipeline"] = skipped.get("renewal_pipeline", 0) + 1
            continue
        if is_archived_hs_row(deal):
            skipped["archived"] = skipped.get("archived", 0) + 1
            continue
        contacts = hs.contacts_for_deal(str(deal.get("id") or ""))
        if not contacts:
            skipped["no_contact"] = skipped.get("no_contact", 0) + 1
            continue
        for contact in contacts:
            fields = _contact_fields(contact)
            key = (fields["email"] or fields["name"]).strip().lower()
            if not key or key in seen:
                continue
            cprops = contact.get("properties") or {}
            createdate = str(props.get("createdate") or cprops.get("createdate") or "")
            lastmod = str(
                props.get("hs_lastmodifieddate")
                or cprops.get("hs_lastmodifieddate")
                or ""
            )
            meeting_at = (
                cprops.get("engagements_last_meeting_booked")
                or props.get("engagements_last_meeting_booked")
                or ""
            )
            meeting_date_source = "hs_meeting_prop" if meeting_at else ""
            if callable(last_meeting) and contact.get("id"):
                stamp = last_meeting(str(contact.get("id") or ""))
                if stamp:
                    meeting_at = stamp.isoformat()
                    meeting_date_source = "hs_meeting"
            if (
                meeting_at
                and createdate
                and meeting_date_source not in TRUSTED_MEETING_DATE_SOURCES
                and _same_calendar_day(meeting_at, createdate)
            ):
                meeting_at = ""
                meeting_date_source = ""
            snippet = (
                fields["snippet"]
                or str(props.get("description") or "")
                or str(props.get("nurture_reason") or "")
            )
            gmail_messages = lookup_gmail_meeting_messages(gmail, email=fields["email"])
            extra = {
                "deal_stage": STAGE["nurture"],
                "closed_won_domains": won_domains,
                "has_renewal_deal": fields["email"].lower() in renewal_emails,
                "closed_won": fields["email"].lower() in won_emails,
                "archived": is_archived_hs_row(contact),
                "createdate": createdate,
                "hs_lastmodifieddate": lastmod,
                "meeting_at": meeting_at,
                "meeting_date_source": meeting_date_source,
                "engagements_last_meeting_booked": meeting_at,
                "no_show_count": props.get("no_show_count") or cprops.get("no_show_count"),
                "last_touch_snippet": snippet,
                "hs_meeting": bool(meeting_at) and meeting_date_source in TRUSTED_MEETING_DATE_SOURCES,
                "fireflies": bool(cprops.get("crm_source") == "fireflies"),
                "cube_acr": bool(cprops.get("crm_source") == "cube_acr"),
                "fireflies_id": str(cprops.get("fireflies_id") or ""),
                "cube_id": str(cprops.get("cube_id") or ""),
                "fireflies_at": str(
                    cprops.get("fireflies_at")
                    or cprops.get("hs_call_start")
                    or ""
                ),
                "cube_at": str(cprops.get("cube_at") or ""),
                "transcript": str(cprops.get("transcript") or ""),
                "summary": str(cprops.get("hs_call_summary") or cprops.get("meeting_summary") or ""),
                "gmail_messages": gmail_messages,
                "meeting_source": (
                    "fireflies"
                    if cprops.get("crm_source") == "fireflies"
                    else "cube_acr"
                    if cprops.get("crm_source") == "cube_acr"
                    else ""
                ),
                NURTURE_THREAD_PROP: fields["nurture_thread_id"]
                or str(props.get(NURTURE_THREAD_PROP) or ""),
                NURTURE_SUBJECT_PROP: fields["nurture_thread_subject"]
                or str(props.get(NURTURE_SUBJECT_PROP) or ""),
            }
            gmail_ev = gmail_meeting_evidence({"extra": extra, "gmail_messages": gmail_messages})
            if gmail_ev.get("future_booking"):
                skipped["future_booking"] = skipped.get("future_booking", 0) + 1
                continue
            recording = bool(extra.get("fireflies") or extra.get("cube_acr") or extra.get("fireflies_id") or extra.get("cube_id"))
            if recording and meeting_at and not extra.get("fireflies_at") and not extra.get("cube_at"):
                extra["fireflies_at"] = meeting_at
                extra["meeting_date_source"] = extra.get("meeting_source") or "fireflies"
            if gmail_ev.get("held_at") and not recording:
                extra["held_at"] = gmail_ev["held_at"].isoformat()
                extra["gmail_followup_at"] = extra["held_at"]
                extra["met"] = True
                extra["meeting_date_source"] = "gmail_followup"
            elif gmail_ev.get("held_at") and recording:
                extra["met"] = True
            elif gmail_ev.get("booking_at") and not recording:
                extra["gmail_booking_at"] = gmail_ev["booking_at"].isoformat()
                extra["booked"] = True
                extra["meeting_date_source"] = extra.get("meeting_date_source") or "gmail_booking"
                if not meeting_at:
                    meeting_at = extra["gmail_booking_at"]
                    extra["meeting_at"] = meeting_at
            blocked = is_not_deal_candidate(
                name=fields["name"],
                email=fields["email"],
                company=fields["company"],
                phone=fields["phone"],
                contact=contact,
                deals=[deal],
                extra=extra,
            )
            if not blocked and fields["email"].lower() in won_emails:
                blocked = "closed_won"
            if not blocked and email_domain(fields["email"]) in won_domains:
                blocked = "closed_won"
            if not blocked and fields["email"].lower() in renewal_emails:
                blocked = "client"
            if blocked:
                skipped[blocked] = skipped.get(blocked, 0) + 1
                continue
            seen.add(key)
            company = nurture_company_label(
                fields["company"], str(props.get("dealname") or ""), fields["email"]
            )
            company_industry = ""
            getter = getattr(hs, "associated_company_industry", None)
            if callable(getter):
                company_industry = str(
                    getter(
                        contact_id=str(contact.get("id") or ""),
                        deal_id=str(deal.get("id") or ""),
                    )
                    or ""
                )
            hs_industry = company_industry or fields["industry"]
            industry, _basis = infer_industry_resolved(
                email=fields["email"],
                company=company or fields["company"],
                website_text="",
                hs_industry=hs_industry,
            )
            candidates.append(
                {
                    "name": fields["name"] or str(props.get("dealname") or "").split(" - ")[0].strip(),
                    "email": fields["email"],
                    "company": company,
                    "dealname": str(props.get("dealname") or ""),
                    "phone": fields["phone"],
                    "source": "hubspot",
                    "deal_stage": STAGE["nurture"],
                    "hs_deal_id": str(deal.get("id") or ""),
                    "hs_contact_id": str(contact.get("id") or ""),
                    "reason": infer_nurture_reason(
                        deal_stage=STAGE["nurture"],
                        extra=extra,
                    ),
                    "meeting_at": meeting_at,
                    "meeting_date_source": extra.get("meeting_date_source") or "",
                    "createdate": createdate,
                    "hs_lastmodifieddate": lastmod,
                    "gmail_messages": gmail_messages,
                    "gmail_followup_at": extra.get("gmail_followup_at") or "",
                    "gmail_booking_at": extra.get("gmail_booking_at") or "",
                    "held_at": extra.get("held_at") or "",
                    "last_touch_snippet": snippet,
                    "industry": industry,
                    "hs_industry": hs_industry,
                    "fireflies": extra.get("fireflies"),
                    "cube_acr": extra.get("cube_acr"),
                    "fireflies_id": extra.get("fireflies_id") or "",
                    "cube_id": extra.get("cube_id") or "",
                    "fireflies_at": extra.get("fireflies_at") or "",
                    "cube_at": extra.get("cube_at") or "",
                    "transcript": extra.get("transcript") or "",
                    "summary": extra.get("summary") or "",
                    "meeting_source": extra.get("meeting_source") or "",
                    NURTURE_THREAD_PROP: extra.get(NURTURE_THREAD_PROP) or "",
                    NURTURE_SUBJECT_PROP: extra.get(NURTURE_SUBJECT_PROP) or "",
                    "signal_at": (
                        meeting_at
                        or props.get("notes_last_contacted")
                        or props.get("hs_last_sales_activity_timestamp")
                        or props.get("createdate")
                        or ""
                    ),
                    "extra": extra,
                    "met": extra.get("met") or extra.get("fireflies") or extra.get("cube_acr"),
                    "booked": extra.get("booked"),
                }
            )
    attached = [attach_gmail_thread(row) for row in candidates]
    picked, dropped = _pick_one_per_company(attached)
    if dropped:
        skipped["same_company"] = skipped.get("same_company", 0) + dropped
    picked.sort(key=_card_rank, reverse=True)
    cards: list[dict[str, Any]] = []
    for row in picked[:limit]:
        draft = compose_nurture_draft(row)
        cards.append(
            {
                "name": row["name"],
                "email": row["email"],
                "reason": row["reason"],
                "subject": draft.subject,
                "thread_id": stored_nurture_thread_id(row),
                "body": draft.body,
                "source_id": draft.spoken_source_id or draft.meeting_source_id,
            }
        )
    payload = {
        "count": len(cards),
        "source": "hubspot_nurture",
        "dealstage": STAGE["nurture"],
        "skipped": skipped,
        "cards": cards,
    }
    path = Path(out_path) if out_path else Path("artifacts") / "nurture_hubspot_sample_cards.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")
    payload["out_path"] = str(path)
    return payload
