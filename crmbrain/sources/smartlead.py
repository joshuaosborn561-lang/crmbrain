from __future__ import annotations

from datetime import datetime, timezone

import requests

from crmbrain.config import POSITIVE_SMARTLEAD_CATEGORIES, POSITIVE_SENTIMENTS, Settings
from crmbrain.models import Engagement


def _get(settings: Settings, path: str, params: dict | None = None) -> dict | list:
    merged = {"api_key": settings.smartlead_key, **(params or {})}
    resp = requests.get(f"https://server.smartlead.ai/{path}", params=merged, timeout=40)
    resp.raise_for_status()
    return resp.json()


def categories(settings: Settings) -> dict[int, dict]:
    rows = _get(settings, "api/v1/leads/fetch-categories")
    return {int(r["id"]): r for r in rows}


def campaigns(settings: Settings) -> list[dict]:
    rows = _get(settings, "api/v1/campaigns")
    return rows if isinstance(rows, list) else []


def scan(settings: Settings) -> list[Engagement]:
    """Any positive SalesGlider reply. The key already scopes to SG campaigns."""
    cats = categories(settings)
    positive_ids = {
        cid
        for cid, row in cats.items()
        if cid in POSITIVE_SMARTLEAD_CATEGORIES
        or (row.get("sentiment_type") or "").lower() in POSITIVE_SENTIMENTS
    }
    out: list[Engagement] = []
    for camp in campaigns(settings):
        cid = camp.get("id")
        if not cid:
            continue
        for cat_id in sorted(positive_ids):
            payload = _get(
                settings,
                f"api/v1/campaigns/{cid}/leads",
                {"lead_category_id": cat_id, "limit": 100, "offset": 0},
            )
            rows = payload.get("data") if isinstance(payload, dict) else payload
            for row in rows or []:
                lead = row.get("lead") or {}
                email = lead.get("email") or ""
                if not email:
                    continue
                created = row.get("created_at") or lead.get("created_at")
                occurred = None
                if created:
                    try:
                        occurred = datetime.fromisoformat(created.replace("Z", "+00:00"))
                    except ValueError:
                        occurred = None
                out.append(
                    Engagement(
                        source="smartlead",
                        external_id=str(row.get("campaign_lead_map_id") or lead.get("id") or email),
                        occurred_at=occurred or datetime.now(timezone.utc),
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
                )
    return out
