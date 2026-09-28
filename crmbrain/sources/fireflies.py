from __future__ import annotations

from datetime import datetime, timezone

import requests

from crmbrain.config import Settings, is_internal_meeting, settings_lookback_start
from crmbrain.models import Engagement
from crmbrain.names import looks_like_meeting_title, parse_attendee_token, person_name_from_attendee
from crmbrain.sources.gmail_scan import is_junk_crm_email, is_notetaker_email

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
    meeting_attendees { displayName email name }
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
    meeting_attendees { displayName email name }
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


def _safe_listing(settings: Settings, limit: int) -> list[dict]:
    try:
        return _post(settings, QUERY, {"limit": limit}).get("transcripts") or []
    except Exception:
        fallback = QUERY.replace(
            "    meeting_attendees { displayName email name }\n",
            "",
        )
        return _post(settings, fallback, {"limit": limit}).get("transcripts") or []


def _safe_detail(settings: Settings, transcript_id: str, row: dict) -> dict:
    try:
        return _post(settings, DETAIL, {"id": transcript_id}).get("transcript") or row
    except Exception:
        fallback = DETAIL.replace(
            "    meeting_attendees { displayName email name }\n",
            "",
        )
        return _post(settings, fallback, {"id": transcript_id}).get("transcript") or row


def counterpart_from_fireflies(
    title: str,
    participants: list,
    attendees: list | None = None,
) -> tuple[str, str, str, str]:
    """Return (name, first, last, email). Never uses the meeting title as a name."""
    del title
    emails: list[str] = []
    display_by_email: dict[str, str] = {}
    for raw in attendees or []:
        if not isinstance(raw, dict):
            display, email = parse_attendee_token(str(raw))
        else:
            email = (raw.get("email") or "").strip().lower()
            display = (raw.get("displayName") or raw.get("name") or "").strip()
        if not email or is_junk_crm_email(email) or is_notetaker_email(email):
            continue
        if "salesglider" in email:
            continue
        if email not in emails:
            emails.append(email)
        if display and not looks_like_meeting_title(display):
            display_by_email[email] = display
    for raw in participants or []:
        display, email = parse_attendee_token(str(raw))
        token = email or str(raw)
        if "@" not in token:
            continue
        email = token.lower() if "@" in token and " " not in token else email
        if not email or is_junk_crm_email(email) or is_notetaker_email(email):
            continue
        if "salesglider" in email:
            continue
        if email not in emails:
            emails.append(email)
        if display and email not in display_by_email and not looks_like_meeting_title(display):
            display_by_email[email] = display
    email = emails[0] if emails else ""
    first, last = person_name_from_attendee(display_by_email.get(email, ""), email)
    name = f"{first} {last}".strip()
    return name, first, last, email


def scan(settings: Settings, limit: int = 50) -> list[Engagement]:
    start = settings_lookback_start(settings)
    listing = _safe_listing(settings, limit)
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
        detail = _safe_detail(settings, row["id"], row)
        sentences = detail.get("sentences") or []
        text = "\n".join(
            (s.get("text") or s.get("raw_text") or "") for s in sentences
        )[:20000]
        summary = ((detail.get("summary") or {}).get("overview") or "")[:2000]
        attendees = detail.get("meeting_attendees") or row.get("meeting_attendees") or []
        name, first, last, email = counterpart_from_fireflies(title, participants, attendees)
        if not email and not name:
            continue
        out.append(
            Engagement(
                source="fireflies",
                external_id=row["id"],
                occurred_at=occurred,
                name=name,
                first_name=first,
                last_name=last,
                email=email,
                transcript=text,
                summary=summary,
                raw_subject=title,
                extra={
                    "participants": participants,
                    "meeting_attendees": attendees,
                    "action_items": (detail.get("summary") or {}).get("action_items"),
                },
            )
        )
    return out
