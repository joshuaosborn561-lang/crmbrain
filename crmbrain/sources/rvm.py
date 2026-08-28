from __future__ import annotations

from datetime import datetime, timezone

import requests

from crmbrain.config import Settings, lookback_start
from crmbrain.models import Engagement


def scan(settings: Settings) -> list[Engagement]:
    """RVM callback = they called back. That is engagement."""
    if not settings.supabase_url or not settings.supabase_key:
        return []
    start = lookback_start(settings.lookback_hours).isoformat()
    url = f"{settings.supabase_url.rstrip('/')}/rest/v1/rvm_callbacks"
    headers = {
        "apikey": settings.supabase_key,
        "Authorization": f"Bearer {settings.supabase_key}",
    }
    resp = requests.get(
        url,
        headers=headers,
        params={
            "created_at": f"gte.{start}",
            "select": "*",
            "order": "created_at.desc",
            "limit": "100",
        },
        timeout=30,
    )
    if resp.status_code >= 400:
        return []
    out: list[Engagement] = []
    for row in resp.json() or []:
        phone = row.get("from_phone") or ""
        client = (row.get("client_name") or "").lower()
        if client and "salesglider" not in client and client not in {"", "demo client"}:
            # Other clients' RVM callbacks are not Josh's CRM.
            if "demo" in client:
                continue
            continue
        out.append(
            Engagement(
                source="rvm",
                external_id=str(row.get("id") or row.get("call_sid") or phone),
                occurred_at=_ts(row.get("created_at")),
                phone=phone,
                summary=f"RVM callback from {phone} ({row.get('category') or row.get('channel') or 'call'})",
                extra=row,
            )
        )
    return out


def _ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)
