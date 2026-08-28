from __future__ import annotations

import json
import time

from crmbrain.config import Settings
from crmbrain.http_mcp import McpClient
from crmbrain.models import Engagement


def _client(settings: Settings) -> McpClient:
    client = McpClient(settings.enrichment_url, timeout=60)
    client.initialize()
    return client


def _apply_row(ev: Engagement, row: dict) -> Engagement:
    ev.linkedin_url = ev.linkedin_url or row.get("linkedin_url") or row.get("linkedin") or ""
    ev.email = ev.email or row.get("email") or row.get("work_email") or ""
    ev.phone = ev.phone or row.get("cellphone") or row.get("phone") or ""
    ev.company = ev.company or row.get("company_name") or row.get("company") or ""
    ev.title = ev.title or row.get("job_title") or row.get("title") or ""
    ev.domain = ev.domain or row.get("domain") or ""
    if not ev.first_name:
        ev.first_name = row.get("first_name") or ev.first_name
    if not ev.last_name:
        ev.last_name = row.get("last_name") or ev.last_name
    return ev


def enrich(settings: Settings, ev: Engagement) -> Engagement:
    if ev.linkedin_url and ev.email and ev.phone:
        return ev
    if not settings.enrichment_url:
        return ev
    domain = ev.domain
    if not domain and ev.email and "@" in ev.email:
        domain = ev.email.split("@")[1]
        ev.domain = domain
    if not domain:
        return ev
    try:
        client = _client(settings)
        client.call("ensure_client", {"client_tag": settings.enrichment_client_tag, "display_name": "SalesGlider"})
        result = client.call(
            "enrich_waterfall",
            {
                "client_tag": settings.enrichment_client_tag,
                "need": "both",
                "max_tier": "fullenrich",
                "rows": json.dumps(
                    [
                        {
                            "domain": domain,
                            "company_name": ev.company,
                            "first_name": ev.first_name,
                            "last_name": ev.last_name,
                            "email": ev.email,
                        }
                    ]
                ),
            },
        )
        if isinstance(result, dict) and result.get("job_id"):
            result = _wait_job(client, result["job_id"])
        rows = _rows_from_result(result)
        if ev.email:
            for row in rows:
                if (row.get("email") or "").lower() == ev.email.lower():
                    return _apply_row(ev, row)
        if ev.first_name and ev.last_name:
            for row in rows:
                if (row.get("first_name") or "").lower() == ev.first_name.lower() and (
                    row.get("last_name") or ""
                ).lower() == ev.last_name.lower():
                    return _apply_row(ev, row)
        if rows:
            return _apply_row(ev, rows[0])
    except Exception:
        return ev
    return ev


def _wait_job(client: McpClient, job_id: str, attempts: int = 12) -> dict:
    last: dict = {}
    for _ in range(attempts):
        last = client.call("get_job_status", {"job_id": job_id}) or {}
        status = str(last.get("status") or last.get("state") or "").lower()
        if status in {"completed", "complete", "failed", "error"}:
            return last
        time.sleep(5)
    return last


def _rows_from_result(result) -> list[dict]:
    if not isinstance(result, dict):
        return []
    for key in ("contacts", "people", "rows", "results", "items"):
        val = result.get(key)
        if isinstance(val, list):
            return [v for v in val if isinstance(v, dict)]
    if result.get("email") or result.get("linkedin_url"):
        return [result]
    return []
