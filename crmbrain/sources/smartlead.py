from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests

from crmbrain.config import POSITIVE_SMARTLEAD_CATEGORIES, POSITIVE_SENTIMENTS, Settings
from crmbrain.models import Engagement

logger = logging.getLogger(__name__)

RETRYABLE_STATUS = frozenset({429, 503})
MAX_RETRIES = 5
BACKOFF_BASE = 1.0
BACKOFF_CAP = 30.0
PAGE_PAUSE = 0.25
CAMPAIGN_PAUSE = 0.35
LEADS_PAGE_SIZE = 100


def _sleep(seconds: float) -> None:
    if seconds > 0:
        time.sleep(seconds)


def _retry_after_seconds(resp: requests.Response, fallback: float) -> float:
    raw = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
    if raw is None:
        return fallback
    raw = str(raw).strip()
    if not raw:
        return fallback
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return fallback


def _backoff_with_jitter(attempt: int) -> float:
    base = min(BACKOFF_CAP, BACKOFF_BASE * (2**attempt))
    return min(BACKOFF_CAP, base * (0.5 + random.random()))


def _get(settings: Settings, path: str, params: dict | None = None) -> dict | list:
    merged = {"api_key": settings.smartlead_key, **(params or {})}
    url = f"https://server.smartlead.ai/{path}"
    for attempt in range(MAX_RETRIES + 1):
        resp = requests.get(url, params=merged, timeout=40)
        if resp.status_code in RETRYABLE_STATUS and attempt < MAX_RETRIES:
            delay = min(BACKOFF_CAP, _retry_after_seconds(resp, _backoff_with_jitter(attempt)))
            logger.warning(
                "smartlead %s HTTP %s, retry %s/%s in %.2fs",
                path,
                resp.status_code,
                attempt + 1,
                MAX_RETRIES,
                delay,
            )
            _sleep(delay)
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError(f"smartlead {path}: retries exhausted")


def categories(settings: Settings) -> dict[int, dict]:
    rows = _get(settings, "api/v1/leads/fetch-categories")
    return {int(r["id"]): r for r in rows}


def campaigns(settings: Settings) -> list[dict]:
    rows = _get(settings, "api/v1/campaigns")
    return rows if isinstance(rows, list) else []


def _last_reply_at(settings: Settings, campaign_id: int, lead_id: int) -> datetime | None:
    try:
        payload = _get(
            settings,
            f"api/v1/campaigns/{campaign_id}/leads/{lead_id}/message-history",
        )
    except Exception:
        return None
    history = payload.get("history") if isinstance(payload, dict) else payload
    latest = None
    for item in history or []:
        if str(item.get("type") or "").upper() not in {"REPLY", "RECEIVED", "INBOUND"}:
            continue
        raw = item.get("time") or item.get("created_at")
        if not raw:
            continue
        try:
            ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            continue
        if latest is None or ts > latest:
            latest = ts
    return latest


def _iter_campaign_leads(settings: Settings, campaign_id: int, cat_id: int):
    offset = 0
    while True:
        payload = _get(
            settings,
            f"api/v1/campaigns/{campaign_id}/leads",
            {"lead_category_id": cat_id, "limit": LEADS_PAGE_SIZE, "offset": offset},
        )
        rows = payload.get("data") if isinstance(payload, dict) else payload
        rows = list(rows or [])
        yield from rows
        if len(rows) < LEADS_PAGE_SIZE:
            break
        offset += LEADS_PAGE_SIZE
        _sleep(PAGE_PAUSE)


def _engagement_from_row(
    row: dict,
    camp: dict,
    cid: int,
    cat_id: int,
    cats: dict[int, dict],
    settings: Settings,
) -> Engagement | None:
    lead = row.get("lead") or {}
    email = lead.get("email") or ""
    lead_id = lead.get("id")
    if not email or not lead_id:
        return None
    occurred = _last_reply_at(settings, cid, lead_id)
    return Engagement(
        source="smartlead",
        external_id=str(row.get("campaign_lead_map_id") or lead_id or email),
        occurred_at=occurred or datetime.fromtimestamp(0, tz=timezone.utc),
        first_name=lead.get("first_name") or "",
        last_name=lead.get("last_name") or "",
        email=email,
        phone=lead.get("phone_number") or "",
        company=lead.get("company_name") or "",
        linkedin_url=lead.get("linkedin_profile") or "",
        summary=f"Positive SmartLead reply ({cats.get(cat_id, {}).get('name', cat_id)}) in {camp.get('name')}",
        extra={
            "campaign_id": cid,
            "campaign_name": camp.get("name"),
            "lead_category_id": cat_id,
        },
    )


def scan(settings: Settings, errors: list[str] | None = None) -> list[Engagement]:
    """Any positive SalesGlider reply. The key already scopes to SG campaigns."""
    cats = categories(settings)
    positive_ids = {
        cid
        for cid, row in cats.items()
        if cid in POSITIVE_SMARTLEAD_CATEGORIES
        or (row.get("sentiment_type") or "").lower() in POSITIVE_SENTIMENTS
    }
    out: list[Engagement] = []
    active = [
        camp
        for camp in campaigns(settings)
        if camp.get("id") and str(camp.get("status") or "").upper() in {"ACTIVE", "STARTED", "INPROGRESS"}
    ]
    for index, camp in enumerate(active):
        cid = camp.get("id")
        try:
            for cat_id in sorted(positive_ids):
                for row in _iter_campaign_leads(settings, cid, cat_id):
                    ev = _engagement_from_row(row, camp, cid, cat_id, cats, settings)
                    if ev:
                        out.append(ev)
                _sleep(PAGE_PAUSE)
        except Exception as exc:
            msg = f"smartlead campaign {cid}: {exc}"
            logger.warning(msg)
            if errors is not None:
                errors.append(msg)
            else:
                raise
        if index < len(active) - 1:
            _sleep(CAMPAIGN_PAUSE)
    return out
