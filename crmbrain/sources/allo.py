"""Allo phone-call ingest.

Root cause of the Sep 18 stall: crmbrain never read `allo.calls`, and the API
client hit a made-up `{ALLO_API_URL}/conversations` path with `Bearer` auth.
Allo v2 is `https://api.withallo.com` and authenticates as `Authorization: Api-Key`.
The table was filled by a one-shot dump on 2026-09-18 23:01 UTC and never
incremented. This module incremental-syncs via
`POST /v2/api/conversations/items/search` and upserts `allo.calls`.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import requests

from crmbrain.config import Settings, settings_lookback_start
from crmbrain.models import Engagement

logger = logging.getLogger(__name__)

DEFAULT_ALLO_HOST = "https://api.withallo.com"
SEARCH_PATH = "/v2/api/conversations/items/search"
PAGE_SIZE = 100


def _host(settings: Settings) -> str:
    raw = (settings.allo_url or DEFAULT_ALLO_HOST).rstrip("/")
    if raw.endswith("/v2/api"):
        return raw[: -len("/v2/api")]
    return raw or DEFAULT_ALLO_HOST


def _headers(settings: Settings) -> dict[str, str]:
    return {
        "Authorization": f"Api-Key {settings.allo_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _parse_dt(value: object) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    raw = str(value).replace("Z", "+00:00")
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def _transcript_text(raw: object) -> str:
    if not raw:
        return ""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        parts = []
        for row in raw:
            if isinstance(row, dict):
                parts.append(str(row.get("text") or row.get("content") or ""))
            else:
                parts.append(str(row))
        return "\n".join(p for p in parts if p)
    return str(raw)


def row_to_engagement(row: dict) -> Engagement:
    contacts = row.get("contacts") or []
    contact = contacts[0] if contacts and isinstance(contacts[0], dict) else {}
    extracted = ((row.get("extracted_data") or {}).get("contact") or {}) if isinstance(row.get("extracted_data"), dict) else {}
    name = (
        row.get("contact_name")
        or contact.get("name")
        or extracted.get("name")
        or ""
    )
    company = row.get("contact_company") or ((contact.get("company") or {}).get("name") if isinstance(contact.get("company"), dict) else "") or extracted.get("company") or ""
    first, last = "", ""
    if name:
        bits = str(name).split()
        first, last = bits[0], " ".join(bits[1:])
    transcript = _transcript_text(row.get("transcript"))
    occurred = _parse_dt(row.get("call_at") or row.get("date"))
    return Engagement(
        source="allo",
        external_id=str(row.get("id") or row.get("call_id") or ""),
        occurred_at=occurred,
        first_name=first,
        last_name=last,
        name=str(name),
        email=extracted.get("emails")[0] if extracted.get("emails") else "",
        phone=row.get("contact_number") or "",
        company=str(company or ""),
        summary=row.get("summary") or "",
        transcript=transcript[:20000],
        raw_subject=f"Allo {row.get('direction') or ''} {name}".strip(),
        extra={
            "duration": row.get("duration") or 0,
            "result": row.get("result") or "",
            "direction": row.get("direction") or "",
            "tags": row.get("tags") or [],
        },
    )


def _usable_call(row: dict) -> bool:
    """Josh actually talked — skip 3-second closed/voicemail blasts."""
    duration = int(row.get("duration") or 0)
    transcript = _transcript_text(row.get("transcript"))
    summary = (row.get("summary") or "").strip()
    result = str(row.get("result") or "").upper()
    if duration >= 45 and (transcript or summary):
        return True
    if result in {"ANSWERED", "TRANSFERRED_EXTERNAL", "TRANSFERRED"} and (transcript or summary or duration >= 30):
        return True
    return False


def fetch_items_since(settings: Settings, since: datetime, errors: list[str] | None = None) -> list[dict]:
    """Page Allo search from `since` through now. Raises nothing; appends errors."""
    if not settings.allo_key:
        if errors is not None:
            errors.append("allo: ALLO_API_KEY missing — cannot incremental-sync")
        return []
    host = _host(settings)
    url = host + SEARCH_PATH
    out: list[dict] = []
    page = 1
    while page <= 20:
        payload = {
            "type": "CALL",
            "date": {"from": since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")},
            "page": page,
            "size": PAGE_SIZE,
        }
        try:
            resp = requests.post(url, headers=_headers(settings), json=payload, timeout=40)
        except Exception as exc:
            msg = f"allo search: {exc}"
            logger.warning(msg)
            if errors is not None:
                errors.append(msg)
            break
        if resp.status_code in {401, 403}:
            msg = (
                f"allo API {resp.status_code} — reconnect the Allo API key "
                "(Authorization: Api-Key, Settings > API at withallo.com)"
            )
            logger.warning(msg)
            if errors is not None:
                errors.append(msg)
            break
        if resp.status_code >= 400:
            msg = f"allo search HTTP {resp.status_code}: {resp.text[:240]}"
            logger.warning(msg)
            if errors is not None:
                errors.append(msg)
            break
        body = resp.json() if resp.content else {}
        rows = body.get("data") or body.get("items") or []
        out.extend(r for r in rows if isinstance(r, dict))
        pagination = body.get("pagination") or {}
        if not pagination.get("has_more") and len(rows) < PAGE_SIZE:
            break
        page += 1
    return out


def upsert_calls(memory, rows: list[dict]) -> int:
    written = 0
    if not hasattr(memory, "upsert_allo_call"):
        return 0
    for row in rows:
        try:
            memory.upsert_allo_call(_row_for_table(row))
            written += 1
        except Exception as exc:
            logger.warning("allo upsert %s: %s", row.get("id"), exc)
    return written


def _row_for_table(row: dict) -> dict[str, Any]:
    contact = (row.get("contacts") or [None])[0] or {}
    extracted = ((row.get("extracted_data") or {}).get("contact") or {}) if isinstance(row.get("extracted_data"), dict) else {}
    user = row.get("user") or {}
    return {
        "id": str(row.get("id") or ""),
        "direction": row.get("direction"),
        "allo_number": row.get("allo_number"),
        "contact_number": row.get("contact_number"),
        "user_id": user.get("id"),
        "user_name": user.get("name"),
        "call_at": row.get("date") or row.get("call_at"),
        "duration": row.get("duration"),
        "result": row.get("result"),
        "summary": row.get("summary"),
        "tags": row.get("tags") or [],
        "contact_name": contact.get("name") or extracted.get("name"),
        "contact_company": ((contact.get("company") or {}).get("name") if isinstance(contact.get("company"), dict) else None)
        or extracted.get("company"),
        "transcript": row.get("transcript") or [],
        "raw": row,
        "synced_at": datetime.now(timezone.utc).isoformat(),
    }


def load_table_since(memory, since: datetime) -> list[dict]:
    if not hasattr(memory, "list_allo_calls"):
        return []
    try:
        return memory.list_allo_calls(since)
    except Exception as exc:
        logger.warning("allo table read: %s", exc)
        return []


def scan(settings: Settings, gmail=None, memory=None, errors: list[str] | None = None) -> list[Engagement]:
    """Incremental Allo API sync → allo.calls, then emit talked-to engagements."""
    start = settings_lookback_start(settings)
    api_since = start
    if memory and hasattr(memory, "latest_allo_call_at"):
        last = memory.latest_allo_call_at()
        if last:
            api_since = last
    fetched: list[dict] = []
    if settings.allo_key:
        fetched = fetch_items_since(settings, api_since, errors=errors)
        if memory and fetched:
            upsert_calls(memory, fetched)
    table_rows = load_table_since(memory, start) if memory else []
    by_id: dict[str, dict] = {}
    for row in table_rows + fetched:
        cid = str(row.get("id") or "")
        if cid:
            by_id[cid] = row
    out: list[Engagement] = []
    for row in by_id.values():
        if not _usable_call(row):
            continue
        ev = row_to_engagement(row)
        if ev.external_id:
            out.append(ev)
    if gmail:
        out.extend(_gmail_fallback(settings, gmail))
    return out


def _gmail_fallback(settings: Settings, gmail) -> list[Engagement]:
    from crmbrain.config import gmail_after_clause

    after = gmail_after_clause(settings_lookback_start(settings))
    out: list[Engagement] = []
    try:
        stubs = gmail.search(
            f'{after} (from:allo.ai OR from:withallo.com OR from:callallo.com OR from:hello@allo.com OR subject:"Allo call")',
            max_results=20,
        )
    except Exception:
        return []
    for stub in stubs:
        try:
            msg = gmail.get(stub["id"])
        except Exception:
            continue
        headers = gmail.headers_map(msg)
        subject = headers.get("subject", "")
        snippet = msg.get("snippet", "")
        out.append(
            Engagement(
                source="allo",
                external_id=stub["id"],
                occurred_at=datetime.fromtimestamp(int(msg.get("internalDate", "0")) / 1000, tz=timezone.utc),
                raw_subject=subject,
                summary=snippet,
            )
        )
    return out
