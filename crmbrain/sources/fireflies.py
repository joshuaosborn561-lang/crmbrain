from __future__ import annotations

from datetime import datetime, timezone

import requests

from crmbrain.config import Settings, is_internal_meeting, lookback_start
from crmbrain.models import Engagement

QUERY = """
query Transcripts($limit: Int) {
  transcripts(limit: $limit) {
    id
    title
    date
    duration
    host_email
    organizer_email
    participants
    transcript_url
    summary { overview action_items shorthand_bullet }
  }
}
"""

DETAIL = """
query Transcript($id: String!) {
  transcript(id: $id) {
    id
    title
    date
    participants
    sentences { speaker_id raw_text text }
    summary { overview action_items shorthand_bullet }
  }
}
"""


def _post(settings: Settings, query: str, variables: dict) -> dict:
    resp = requests.post(
        "https://api.fireflies.ai/graphql",
        headers={
            "Authorization": f"Bearer {settings.fireflies_key}",
            "Content-Type": "application/json",
        },
        json={"query": query, "variables": variables},
        timeout=45,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("errors"):
        raise RuntimeError(data["errors"])
    return data.get("data") or {}


def scan(settings: Settings) -> list[Engagement]:
    start = lookback_start(settings.lookback_hours)
    listing = _post(settings, QUERY, {"limit": 50}).get("transcripts") or []
    out: list[Engagement] = []
    for row in listing:
        ms = row.get("date") or 0
        occurred = datetime.fromtimestamp(ms / 1000, tz=timezone.utc) if ms else None
        if occurred and occurred < start:
            continue
        title = row.get("title") or ""
        participants = row.get("participants") or []
        if is_internal_meeting(title, participants):
            continue
        detail = _post(settings, DETAIL, {"id": row["id"]}).get("transcript") or row
        sentences = detail.get("sentences") or []
        text = "\n".join(
            (s.get("text") or s.get("raw_text") or "") for s in sentences
        )[:20000]
        summary = ((detail.get("summary") or {}).get("overview") or "")[:2000]
        emails = [p for p in participants if "@" in p and "salesglider" not in p.lower()]
        name = title.replace(" and Joshua Osborn", "").replace("/Josh Osborn", "").strip()
        out.append(
            Engagement(
                source="fireflies",
                external_id=row["id"],
                occurred_at=occurred,
                name=name,
                email=emails[0] if emails else "",
                transcript=text,
                summary=summary,
                raw_subject=title,
                extra={"participants": participants, "action_items": (detail.get("summary") or {}).get("action_items")},
            )
        )
    return out
