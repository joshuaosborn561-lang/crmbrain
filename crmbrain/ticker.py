from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from crmbrain.config import STAGE, digits_phone, now_utc
from crmbrain.intelligence import heuristic_extract
from crmbrain.memory import Memory
from crmbrain.models import Engagement

TICKER_DAYS = 90
REASON_RANK = {"no_show": 3, "kicked_can": 2, "deal_died": 2, "never_booked": 1}
MEETING_STAGES = {
    STAGE["discovery_scheduled"],
    STAGE["discovery_completed"],
    STAGE["proposal_sent"],
    STAGE["signed"],
    STAGE["paid"],
}


@dataclass
class TickerCandidate:
    name: str
    email: str = ""
    phone: str = ""
    company: str = ""
    reason: str = "never_booked"
    last_signal: datetime | None = None
    hs_contact_id: str = ""
    hs_deal_id: str = ""
    source: str = ""
    skip_reason: str = ""
    extra: dict = field(default_factory=dict)


def parse_signal_at(value) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    raw = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def next_fire_from_signal(last_signal, now: datetime | None = None) -> str:
    """last_signal + 90d, or now when that instant is already in the past."""
    now = now or now_utc()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    signal = parse_signal_at(last_signal) or now
    candidate = signal + timedelta(days=TICKER_DAYS)
    if candidate <= now:
        return now.isoformat()
    return candidate.isoformat()


def already_enrolled(
    ticker_rows: list[dict],
    *,
    email: str = "",
    phone: str = "",
    hs_contact_id: str = "",
) -> bool:
    email_l = (email or "").strip().lower()
    phone_d = digits_phone(phone)
    hs = str(hs_contact_id or "").strip()
    for row in ticker_rows:
        if email_l and (row.get("email") or "").strip().lower() == email_l:
            return True
        if hs and str(row.get("hs_contact_id") or "").strip() == hs:
            return True
        other = digits_phone(row.get("phone") or "")
        if phone_d and len(phone_d) >= 10 and other and other[-10:] == phone_d[-10:]:
            return True
    return False


def classify_reason(*, stage: str = "", hint: str = "", text: str = "") -> str:
    if hint in REASON_RANK:
        return hint
    if stage in {STAGE["no_show"], "no_show"}:
        return "no_show"
    facts = heuristic_extract(text or "")
    if facts.get("ticker_reason") in REASON_RANK:
        return facts["ticker_reason"]
    return "never_booked"


def in_live_meeting_stage(stage: str) -> bool:
    return stage in MEETING_STAGES


def candidate_key(c: TickerCandidate) -> str:
    if c.email:
        return f"email:{c.email.strip().lower()}"
    if c.hs_contact_id:
        return f"hs:{c.hs_contact_id}"
    phone_d = digits_phone(c.phone)
    if len(phone_d) >= 10:
        return f"phone:{phone_d[-10:]}"
    return f"name:{(c.name or '').strip().lower()}"


def plan_enrollments(
    candidates: list[TickerCandidate],
    existing_ticker: list[dict],
    now: datetime | None = None,
) -> tuple[list[dict], list[TickerCandidate]]:
    """Build ticker rows. Does not write. Skips anyone already on the ticker."""
    now = now or now_utc()
    skipped: list[TickerCandidate] = []
    kept: dict[str, TickerCandidate] = {}
    for raw in candidates:
        c = raw
        if already_enrolled(
            existing_ticker,
            email=c.email,
            phone=c.phone,
            hs_contact_id=c.hs_contact_id,
        ):
            c.skip_reason = "already_on_ticker"
            skipped.append(c)
            continue
        if not (c.email or c.phone or c.hs_contact_id or c.name):
            c.skip_reason = "no_identity"
            skipped.append(c)
            continue
        key = candidate_key(c)
        prev = kept.get(key)
        if prev is None:
            kept[key] = c
            continue
        prev_rank = REASON_RANK.get(prev.reason, 0)
        new_rank = REASON_RANK.get(c.reason, 0)
        prev_ts = parse_signal_at(prev.last_signal) or datetime.min.replace(tzinfo=timezone.utc)
        new_ts = parse_signal_at(c.last_signal) or datetime.min.replace(tzinfo=timezone.utc)
        if new_rank > prev_rank or (new_rank == prev_rank and new_ts > prev_ts):
            kept[key] = c
    rows = [
        backfill_row(
            name=c.name,
            email=c.email,
            phone=c.phone,
            company=c.company,
            reason=c.reason,
            last_signal=c.last_signal,
            hs_contact_id=c.hs_contact_id,
            hs_deal_id=c.hs_deal_id,
            now=now,
        )
        for c in kept.values()
    ]
    return rows, skipped


def backfill_row(
    *,
    name: str,
    email: str = "",
    phone: str = "",
    company: str = "",
    reason: str,
    last_signal,
    hs_contact_id: str = "",
    hs_deal_id: str = "",
    now: datetime | None = None,
) -> dict:
    return {
        "id": str(uuid4()),
        "name": name or "",
        "email": email or None,
        "phone": phone or None,
        "company": company or None,
        "hs_contact_id": hs_contact_id or None,
        "hs_deal_id": hs_deal_id or None,
        "reason": reason,
        "status": "active",
        "next_fire_at": next_fire_from_signal(last_signal, now),
    }


def apply_plan(
    memory: Memory,
    candidates: list[TickerCandidate],
    now: datetime | None = None,
    write: bool = False,
) -> dict:
    existing = memory.list_ticker()
    rows, skipped = plan_enrollments(candidates, existing, now=now)
    if write:
        for row in rows:
            memory.enroll_ticker(row)
    by_reason: dict[str, int] = {}
    for row in rows:
        by_reason[row["reason"]] = by_reason.get(row["reason"], 0) + 1
    skip_counts: dict[str, int] = {}
    for c in skipped:
        skip_counts[c.skip_reason] = skip_counts.get(c.skip_reason, 0) + 1
    return {
        "would_enroll": len(rows),
        "enrolled": len(rows) if write else 0,
        "already_on_ticker": skip_counts.get("already_on_ticker", 0),
        "by_reason": by_reason,
        "rows": rows,
        "skipped": skipped,
        "wrote": write,
        "errors": list(memory.errors),
    }


def enroll(memory: Memory, ev: Engagement, reason: str, hs_contact_id: str = "", hs_deal_id: str = "") -> dict:
    next_fire = (now_utc() + timedelta(days=TICKER_DAYS)).isoformat()
    row = {
        "id": str(uuid4()),
        "name": ev.display_name(),
        "email": ev.email or None,
        "phone": ev.phone or None,
        "company": ev.company or None,
        "hs_contact_id": hs_contact_id or None,
        "hs_deal_id": hs_deal_id or None,
        "reason": reason,
        "status": "active",
        "next_fire_at": next_fire,
    }
    memory.enroll_ticker(row)
    return row


def draft_email(name: str, company: str, reason: str) -> tuple[str, str]:
    first = (name or "there").split(" ")[0]
    subject = {
        "no_show": "Still around?",
        "never_booked": "Quick bump",
        "kicked_can": "Timing better?",
        "deal_died": "Worth a look?",
    }.get(reason, "Quick bump")
    body = (
        f"Hey {first},\n\n"
        f"Wanted to bump this in case timing is better on {company or 'your side'}.\n\n"
        "We are still booking qualified meetings for owners... $2M pipeline and $100K closed from the same motion.\n\n"
        "I can send a Loom or throw Airpods at a test list if that makes it easy.\n\n"
        "worth sharing more?"
    )
    if "—" in body or "–" in body or "—" in subject:
        body = body.replace("—", "...").replace("–", "...")
    return subject, body
