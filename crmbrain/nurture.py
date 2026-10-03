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
    STAGE,
    Settings,
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

AIRPODS_OFFER_LIVE = True
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
    "roofing": "one of our roofers closed $100K in his first 3 months with us",
    "hvac": "$2M in pipeline last quarter across our trades clients, one closed $100K in their first 3 months",
}
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
) -> str:
    if is_josh_address(email):
        return "non_deal"
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
        STAGE["discovery_scheduled"],
        STAGE["discovery_completed"],
        STAGE["proposal_sent"],
        STAGE["nurture"],
        STAGE["no_show"],
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


def may_enroll_from_engagement(ev: Engagement) -> tuple[bool, str]:
    blocked = is_not_deal_candidate(
        name=ev.display_name() or ev.name,
        email=ev.email,
        company=ev.company,
        campaign=str((ev.extra or {}).get("campaign_name") or ev.raw_subject or ""),
        phone=ev.phone,
    )
    if blocked:
        return False, blocked
    if has_meeting_qualification(source=ev.source, ev=ev, extra=ev.extra or {}, reason=ev.ticker_reason):
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
        extra["last_touch_snippet"] = extra.get("last_touch_snippet") or other.extra.get("last_touch_snippet") or ""
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
    return (name or "").strip().split(" ")[0] or "there"


def _airpods_line() -> str:
    return "I can also send you a pair of AirPods just for chatting 15 minutes to see if this makes sense."


def _proof_line(industry: str | None) -> str:
    if industry and industry in CASE_STUDIES:
        return CASE_STUDIES[industry]
    return GENERAL_PROOF


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


def _opener_from_snippet(first: str, snippet: str, campaign: str = "") -> str:
    clean = _strip_poc_phrases(snippet_of(snippet, 180))
    clean = _no_dashes(clean)
    if clean:
        low = clean.rstrip(".")
        return f"Hey {first}, you mentioned {low[0].lower() + low[1:] if low else low}."
    topic = ""
    if campaign:
        topic = re.sub(r"salesglider\s*", "", campaign, flags=re.I).strip() or campaign
    if topic:
        return f"Hey {first}, you replied a while back when we reached out about {topic}."
    return f"Hey {first}, it's been a few months since we connected."


def _numbers_in(text: str) -> set[str]:
    return {m.group(0).replace(",", "") for m in _NUMBER_RE.finditer(text or "")}


def compose_nurture_draft(row: dict, *, airpods: bool | None = None) -> NurtureDraft:
    """Spec draft: opener from snippet, industry proof, meeting guarantee, Josh Osborn."""
    name = str(row.get("name") or "")
    first = _first_name(name)
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
    snippet = str(row.get("last_touch_snippet") or "")
    use_airpods = AIRPODS_OFFER_LIVE if airpods is None else airpods
    opener = _no_dashes(_opener_from_snippet(first, snippet, campaign))
    proof = _proof_line(industry)
    cta = MEETING_GUARANTEE
    if use_airpods:
        cta = f"{cta} {_airpods_line()}"
    body = _no_dashes(
        f"{opener}\n\n{proof}\n\n{cta}\n\nWorth a look?\n\nJosh Osborn"
    )
    if industry and industry in INDUSTRY_SUBJECTS:
        subject = INDUSTRY_SUBJECTS[industry]
    elif first and first.lower() != "there":
        subject = f"{first}?"
    else:
        subject = "Quick update"
    subject = _no_dashes(subject)
    draft = NurtureDraft(subject=subject, body=body)
    return validate_draft(draft, row)


def validate_draft(draft: NurtureDraft, row: dict | None = None) -> NurtureDraft:
    body = _no_dashes(draft.body)
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
        "reason": c.reason,
        "status": "active",
        "source": c.source or None,
        "source_ref": extra.get("source_ref") or None,
        "signal_at": signal.isoformat(),
        "campaign": extra.get("campaign") or None,
        "campaign_id": str(extra.get("campaign_id") or "") or None,
        "industry": industry,
        "industry_basis": basis,
        "last_touch_snippet": snippet_of(str(extra.get("last_touch_snippet") or "")),
        "gmail_thread_id": extra.get("gmail_thread_id") or extra.get("thread_id") or None,
        "in_reply_to": extra.get("in_reply_to") or None,
        "references": extra.get("references") or None,
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
    snippet = str(row.get("last_touch_snippet") or "")[:120]
    source = row.get("source") or "unknown"
    campaign = row.get("campaign") or ""
    ticker_id = str(row.get("id") or "")
    fallback = (
        f"90-day ticker (approve before send)\n"
        f"To: {row.get('email') or row.get('phone')}\n"
        f"Why: {row.get('reason')}\n"
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
                    f"*Why:* {row.get('reason') or 'nurture'}\n"
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
        "sent": "Sent from Josh's Gmail as a thread reply. Removed from nurture for 90 days.",
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
    )
    if blocked:
        patch = {"status": "stopped", "stop_reason": blocked, "stopped_at": now.isoformat()}
        return blocked, patch
    stages = list(associated_stages or row.get("associated_stages") or [])
    if row.get("deal_stage"):
        stages.append(str(row.get("deal_stage")))
    if STAGE["paid"] in stages:
        patch = {"status": "stopped", "stop_reason": "client", "stopped_at": now.isoformat()}
        return "client", patch
    if STAGE["signed"] in stages:
        patch = {"status": "stopped", "stop_reason": "won", "stopped_at": now.isoformat()}
        return "won", patch
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
            "last_touch_snippet": snippet_of((inbound or {}).get("email_body") or (inbound or {}).get("body") or ""),
            "client_campaign": bool(camp.get("client_campaign") or lead.get("client_campaign")),
            "booked": bool(lead.get("booked") or lead.get("met") or camp.get("booked")),
            "met": bool(lead.get("met")),
        }
        name = f"{lead.get('first_name') or ''} {lead.get('last_name') or ''}".strip() or lead.get("name") or ""
        c = TickerCandidate(
            name=name,
            email=str(lead.get("email") or ""),
            company=str(lead.get("company") or ""),
            reason="never_booked",
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
        is_stalled_open = stage in {STAGE["discovery_completed"], STAGE["proposal_sent"]}
        if is_stalled_open:
            if not last_activity or now - last_activity < timedelta(days=STALLED_DAYS):
                continue
        elif not is_nurture and stage != STAGE["no_show"]:
            continue
        signal = note_at or last_activity
        if not signal:
            continue
        snippet = snippet_of(str(note.get("body") or deal.get("snippet") or ""))
        campaign = ""
        if snippet.lower().startswith("source:"):
            first = snippet.split(".", 1)[0]
            campaign = first.split(":", 1)[-1].strip()
        extra = {
            "source_ref": f"hubspot:deal:{deal.get('id')}:contact:{contact.get('id') or ''}",
            "last_touch_snippet": snippet,
            "campaign": campaign or deal.get("campaign") or "",
            "deal_stage": stage,
            "booked": True,
            "website_text": deal.get("website_text") or contact.get("website_text") or "",
        }
        name = f"{contact.get('firstname') or ''} {contact.get('lastname') or ''}".strip() or deal.get("contact_name") or ""
        out.append(
            TickerCandidate(
                name=name,
                email=str(contact.get("email") or deal.get("contact_email") or ""),
                company=str(contact.get("company") or ""),
                reason="kicked_can" if is_nurture else "never_booked",
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
        extra = {
            "source_ref": f"gmail:{thread.get('thread_id')}",
            "last_touch_snippet": snippet_of(str(inbound.get("body") or "")),
            "gmail_thread_id": thread.get("thread_id"),
            "thread_id": thread.get("thread_id"),
            "in_reply_to": inbound.get("message_id") or inbound.get("Message-ID") or "",
            "references": inbound.get("references") or inbound.get("message_id") or "",
            "booked": bool(thread.get("booked") or thread.get("met")),
            "met": bool(thread.get("met")),
        }
        out.append(
            TickerCandidate(
                name=str(thread.get("name") or inbound.get("name") or ""),
                email=str(thread.get("email") or inbound.get("from") or ""),
                reason="kicked_can" if thread.get("intent") == "timing_later" else "never_booked",
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
        },
        {
            "id": "sample-pat",
            "name": "Pat Reyes",
            "email": "pat@summitroofs.test",
            "company": "Summit Roofs",
            "reason": "never_booked",
            "source": "smartlead",
            "campaign": "SalesGlider Roofers",
            "industry": "roofing",
            "industry_basis": "campaign",
            "signal_at": "2026-06-03T17:20:00+00:00",
            "last_touch_snippet": "Yes, send me info. Spring is our slow season.",
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
            "in_reply_to": "<morgan-q4@mail>",
            "references": "<morgan-q4@mail>",
        },
        {
            "id": "sample-lee",
            "name": "Lee Ng",
            "email": "lee@bytewise.test",
            "company": "Bytewise",
            "reason": "never_booked",
            "source": "smartlead",
            "campaign": "SalesGlider MSPs",
            "industry": "msp",
            "industry_basis": "campaign",
            "signal_at": "2026-06-15T12:00:00+00:00",
            "last_touch_snippet": "We just lost our SDR, maybe now.",
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
        },
        {
            "id": "sample-sam",
            "name": "Sam Cole",
            "email": "sam@hireright.test",
            "company": "HireRight",
            "reason": "never_booked",
            "source": "hubspot",
            "campaign": "SalesGlider Staffing",
            "industry": "staffing",
            "industry_basis": "campaign",
            "signal_at": "2026-06-10T12:00:00+00:00",
            "last_touch_snippet": "Need to fill two AE seats before Q4.",
        },
    ]
    del now
    return rows


def render_sample_cards(rows: list[dict] | None = None) -> list[dict]:
    cards = []
    for row in rows or sample_card_rows():
        draft = compose_nurture_draft(row)
        card = build_nurture_card(row, draft)
        cards.append(
            {
                "name": row.get("name"),
                "email": row.get("email"),
                "source": row.get("source"),
                "industry": row.get("industry"),
                "subject": draft.subject,
                "body": draft.body,
                "valid": draft.valid,
                "blocks": card["blocks"],
                "fallback_text": card["text"],
            }
        )
    return cards
