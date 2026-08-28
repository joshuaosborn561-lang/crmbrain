from __future__ import annotations

from datetime import datetime, timezone

import requests

from crmbrain.config import Settings
from crmbrain.gmail_client import Gmail
from crmbrain.models import Engagement


def scan(settings: Settings, gmail: Gmail | None = None) -> list[Engagement]:
    """Allo conversations Josh actually took. API if configured, else Allo mail."""
    out: list[Engagement] = []
    if settings.allo_url and settings.allo_key:
        try:
            resp = requests.get(
                settings.allo_url.rstrip("/") + "/conversations",
                headers={"Authorization": f"Bearer {settings.allo_key}"},
                params={"since": "36h"},
                timeout=30,
            )
            if resp.ok:
                rows = resp.json()
                if isinstance(rows, dict):
                    rows = rows.get("conversations") or rows.get("data") or []
                for row in rows or []:
                    if not row.get("talked") and not row.get("duration"):
                        continue
                    out.append(
                        Engagement(
                            source="allo",
                            external_id=str(row.get("id") or row.get("call_id")),
                            name=row.get("name") or "",
                            email=row.get("email") or "",
                            phone=row.get("phone") or "",
                            company=row.get("company") or "",
                            summary=row.get("summary") or "Allo conversation",
                            transcript=row.get("transcript") or "",
                        )
                    )
        except Exception:
            pass
    if gmail:
        for stub in gmail.search(
            'newer_than:2d (from:allo.ai OR from:withallo.com OR from:callallo.com OR subject:"Allo call" OR subject:"You talked")',
            max_results=20,
        ):
            msg = gmail.get(stub["id"])
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
