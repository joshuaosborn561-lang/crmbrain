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

CASE_STUDIES = {
    "roofing": "one of our roofers closed $100K in his first 3 months with us.",
    "hvac": "$2M in pipeline last quarter across our trades clients, one closed $100K in their first 3 months.",
}
OPENER_MAX_WORDS = 20
_TRANSCRIPT_FIRST_PERSON_RE = re.compile(
    r"\b(i|i'm|i’m|i'll|i’ll|i'd|i’d|i've|i’ve|lets|let's|let’s)\b",
    re.I,
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
GENERAL_PROOF = (
    "$2M in pipeline last quarter, one client closed $100K in their first 3 months, "
    "averaging 14+ replies per month."
)
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

    def as_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "body": self.body,
            "valid": self.valid,
            "reject_reason": self.reject_reason,
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


def meeting_evidence_from_extra(extra: dict | None) -> tuple[bool, bool]:
    """Derive (met, booked) from Fireflies/Cube, recap, calendar, HS meeting, no-show count."""
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


def attach_gmail_thread(row: dict, gmail=None) -> dict:
    """Fill thread ids from any-date sent+inbox search. Else mark new_thread."""
    out = dict(row or {})
    existing = str(out.get("gmail_thread_id") or out.get("thread_id") or "")
    if existing:
        out["gmail_thread_id"] = existing
        out["thread_kind"] = "reply"
        return out
    email = str(out.get("email") or "")
    found = None
    finder = getattr(gmail, "find_contact_thread", None) if gmail is not None else None
    if callable(finder) and email:
        try:
            found = finder(email, name=str(out.get("name") or ""))
        except TypeError:
            found = finder(email)
    if found and found.get("thread_id"):
        out["gmail_thread_id"] = found["thread_id"]
        out["thread_id"] = found["thread_id"]
        out["original_subject"] = found.get("original_subject") or out.get("original_subject") or ""
        out["in_reply_to"] = found.get("in_reply_to") or out.get("in_reply_to") or ""
        out["references"] = found.get("references") or out.get("references") or ""
        out["thread_kind"] = "reply"
        return out
    out["gmail_thread_id"] = ""
    out["thread_kind"] = "new_thread"
    return out


def _topic_from_snippet(snippet: str) -> str:
    clean = snippet_of(snippet, 80)
    clean = re.sub(r"^(yes[,.]?\s*|hey\s+\w+[,.]?\s*)", "", clean, flags=re.I)
    part = re.split(r"[.!?]", clean)[0].strip()
    part = re.sub(r"^(i |we |they |you )", "", part, flags=re.I)
    if len(part) > 52:
        part = part[:52].rsplit(" ", 1)[0]
    return part.strip(" ,")


def compose_nurture_subject(row: dict) -> str:
    original = str(row.get("original_subject") or row.get("gmail_subject") or "").strip()
    thread_id = str(row.get("gmail_thread_id") or row.get("thread_id") or "")
    if thread_id and original:
        return thread_reply_headers(original)["Subject"]
    usable = scoped_snippet(_strip_poc_phrases(str(row.get("last_touch_snippet") or "")), row)
    topic = _topic_from_snippet(usable)
    if topic and is_banned_opener_topic(topic, row):
        topic = ""
    name = str(row.get("name") or "").strip()
    if topic and name and _norm_topic(topic) == _norm_topic(name):
        topic = ""
    company = str(row.get("company") or "").strip()
    if company and topic:
        if company.lower() in topic.lower():
            subject = topic
        else:
            subject = f"{company}: {topic}"
        if is_self_or_company_topic(subject, row) or (name and _norm_topic(subject) == _norm_topic(name)):
            subject = f"{company} follow up"
        if len(subject) > 70:
            subject = f"{company} follow up" if company else (f"Following up {topic}" if topic else "Following up")
        return _no_dashes(subject)
    if company:
        return _no_dashes(f"{company} follow up")
    if topic and not is_banned_opener_topic(topic, row):
        return _no_dashes(f"Following up {topic}" if len(topic.split()) < 4 else topic)
    return "Following up"


def _airpods_line() -> str:
    return "I can also send you a pair of AirPods just for chatting 15 minutes to see if this makes sense."


def _ensure_sentence_period(text: str) -> str:
    out = (text or "").rstrip()
    if out and out[-1] not in ".!?":
        out += "."
    return out


def _proof_line(industry: str | None) -> str:
    if industry and industry in CASE_STUDIES:
        return _ensure_sentence_period(CASE_STUDIES[industry])
    return _ensure_sentence_period(GENERAL_PROOF)


def is_bare_domain(text: str) -> bool:
    return bool(_BARE_DOMAIN_RE.fullmatch((text or "").strip()))


def display_company_name(company: str = "", dealname: str = "", email: str = "") -> str:
    """Real company label. Never a bare domain like wrsroof.com."""
    raw = (company or "").strip()
    if raw and not is_bare_domain(raw):
        return raw
    tail = ""
    if " - " in (dealname or ""):
        tail = dealname.split(" - ", 1)[-1].strip()
    if tail and not is_bare_domain(tail) and _norm_topic(tail) not in _STAGE_PIPELINE_NAMES:
        return tail
    del email
    return ""


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
    met = bool(ev_met or row.get("met") or why == "met")
    booked = bool(ev_booked or row.get("booked") or why in {"booked", "no_show"})
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
    return met, booked


def _row_met_or_booked(row: dict | None) -> bool:
    met, booked = _row_meeting_flags(row)
    return met or booked


def _call_date_phrase(row: dict | None) -> str:
    row = row or {}
    extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
    raw = (
        row.get("meeting_at")
        or extra.get("meeting_at")
        or row.get("call_at")
        or extra.get("call_at")
        or extra.get("hs_meeting_start")
        or extra.get("engagements_last_meeting_booked")
        or extra.get("last_meeting_at")
    )
    dt = parse_signal_at(raw)
    if not dt:
        return ""
    return dt.astimezone(CDT).strftime("%b %-d")


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
    """One short Josh-voice clause, or empty to fall back to the date-call opener."""
    raw = _FIT_NOTE_RE.sub("", text or "").strip()
    low = raw.lower()
    if not low:
        return ""
    for needle, clause in _WANT_TOPIC_CLAUSES:
        if needle in low:
            if spoken_clause_is_raw_transcript(clause):
                return ""
            return clause
    want = re.search(r"wants to ([^.]+)", raw, flags=re.I)
    if want:
        clause = f"you wanted to {want.group(1).strip()}"
        if not spoken_clause_is_raw_transcript(clause):
            return clause
    return ""


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
    usable = scoped_snippet(_strip_poc_phrases(snippet), row)
    usable = _no_dashes(_strip_crm_prefix(usable))
    if usable and is_banned_opener_topic(usable, row):
        usable = ""
    if not usable:
        usable = summarize_spoken_want(snippet) and snippet or ""
        if usable:
            usable = summarize_spoken_want(snippet)
    spoken = ""
    summarized = False
    if usable and usable.lower().startswith("you "):
        spoken = usable
        summarized = True
    elif usable:
        if spoken_clause_is_raw_transcript(usable):
            spoken = summarize_spoken_want(usable)
            summarized = bool(spoken)
        else:
            low = usable.rstrip(".")
            spoken = low[0].lower() + low[1:] if low else low
            if spoken_clause_is_raw_transcript(spoken):
                spoken = summarize_spoken_want(usable)
                summarized = bool(spoken)
    if not spoken:
        spoken = summarize_spoken_want(snippet)
        summarized = bool(spoken)
    met, booked = _row_meeting_flags(row)
    date_phrase = _call_date_phrase(row)
    if met:
        if spoken and summarized:
            if date_phrase:
                return f"Hey {first}, on our {date_phrase} call {spoken}."
            return f"Hey {first}, on our call {spoken}."
        if spoken:
            if date_phrase:
                return f"Hey {first}, on our {date_phrase} call you mentioned {spoken}."
            return f"Hey {first}, on our call you mentioned {spoken}."
        if date_phrase:
            return f"Hey {first}, following up on our {date_phrase} call."
        return f"Hey {first}, following up on our call."
    if booked:
        if spoken and summarized:
            if date_phrase:
                return f"Hey {first}, circling back on the {date_phrase} meeting we had booked. {spoken[0].upper() + spoken[1:]}."
            return f"Hey {first}, circling back on the meeting we had booked. {spoken[0].upper() + spoken[1:]}."
        if date_phrase:
            return f"Hey {first}, circling back on the {date_phrase} meeting we had booked."
        return f"Hey {first}, circling back on the meeting we had booked."
    if spoken and summarized:
        return f"Hey {first}, {spoken}."
    if spoken:
        return f"Hey {first}, you mentioned {spoken}."
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
    if company:
        row = {**row, "company": company}
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
    opener = _no_dashes(_opener_from_snippet(first, raw_snippet, campaign, row))
    proof = _proof_line(industry)
    cta = MEETING_GUARANTEE
    if use_airpods:
        cta = f"{cta} {_airpods_line()}"
    body = capitalize_body_lines(
        _no_dashes(f"{opener}\n\n{proof}\n\n{cta}\n\nWorth a look?\n\nJosh Osborn")
    )
    subject = compose_nurture_subject({**row, "last_touch_snippet": snippet})
    draft = NurtureDraft(subject=subject, body=body)
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
        "gmail_thread_id": extra.get("gmail_thread_id") or extra.get("thread_id") or None,
        "original_subject": extra.get("original_subject") or extra.get("gmail_subject") or None,
        "thread_kind": extra.get("thread_kind") or ("reply" if extra.get("gmail_thread_id") or extra.get("thread_id") else "new_thread"),
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
    fallback = (
        f"90-day ticker (approve before send)\n"
        f"To: {row.get('email') or row.get('phone')}\n"
        f"Why: {why}\n"
        f"Thread: {thread_label}\n"
        f"Source: {source} / {campaign}\n"
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
                    f"*Signal:* {signal_line}\n"
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
    return {"text": fallback, "blocks": blocks, "subject": draft.subject, "body": draft.body}


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
            "gmail_thread_id": thread.get("thread_id"),
            "thread_id": thread.get("thread_id"),
            "original_subject": thread.get("subject") or inbound.get("subject") or "",
            "thread_kind": "reply" if thread.get("thread_id") else "new_thread",
            "in_reply_to": inbound.get("message_id") or inbound.get("Message-ID") or "",
            "references": inbound.get("references") or inbound.get("message_id") or "",
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
    thread = 1 if row.get("gmail_thread_id") or row.get("thread_id") else 0
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
    if hs is None:
        if not getattr(settings, "hubspot_token", ""):
            raise RuntimeError("missing_hubspot_token")
        from crmbrain.hubspot import HubSpot

        hs = HubSpot(settings)
    if gmail is None:
        gmail = _require_gmail(settings)
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
            meeting_at = (
                cprops.get("engagements_last_meeting_booked")
                or props.get("engagements_last_meeting_booked")
                or ""
            )
            if callable(last_meeting) and contact.get("id"):
                stamp = last_meeting(str(contact.get("id") or ""))
                if stamp:
                    meeting_at = stamp.isoformat()
            snippet = (
                fields["snippet"]
                or str(props.get("description") or "")
                or str(props.get("nurture_reason") or "")
            )
            extra = {
                "deal_stage": STAGE["nurture"],
                "closed_won_domains": won_domains,
                "has_renewal_deal": fields["email"].lower() in renewal_emails,
                "closed_won": fields["email"].lower() in won_emails,
                "archived": is_archived_hs_row(contact),
                "meeting_at": meeting_at,
                "engagements_last_meeting_booked": meeting_at,
                "no_show_count": props.get("no_show_count") or cprops.get("no_show_count"),
                "last_touch_snippet": snippet,
                "hs_meeting": bool(meeting_at),
                "fireflies": bool(cprops.get("crm_source") == "fireflies"),
                "cube_acr": bool(cprops.get("crm_source") == "cube_acr"),
            }
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
            company = display_company_name(
                fields["company"], str(props.get("dealname") or ""), fields["email"]
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
                    "last_touch_snippet": snippet,
                    "signal_at": (
                        meeting_at
                        or props.get("notes_last_contacted")
                        or props.get("hs_last_sales_activity_timestamp")
                        or props.get("createdate")
                        or ""
                    ),
                    "extra": extra,
                    "met": extra.get("fireflies") or extra.get("cube_acr"),
                }
            )
    attached = [attach_gmail_thread(row, gmail) for row in candidates]
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
                "thread_id": row.get("gmail_thread_id") or row.get("thread_id") or "",
                "body": draft.body,
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
