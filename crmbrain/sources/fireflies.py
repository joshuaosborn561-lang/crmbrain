from __future__ import annotations

from datetime import datetime, timezone

import requests

from crmbrain.config import Settings, is_internal_meeting, settings_lookback_start
from crmbrain.models import Engagement
from crmbrain.names import looks_like_meeting_title, parse_attendee_token, person_name_from_attendee
from crmbrain.sources.gmail_scan import is_junk_crm_email, is_notetaker_email

EXTRACT_TEXT_CAP = 200_000
MEETING_INFO_BLOCK = "    meeting_info { silent_meeting summary_status }\n"
SPEAKER_NAME_TOKEN = "speaker_name "

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
    meeting_info { silent_meeting summary_status }
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
    meeting_info { silent_meeting summary_status }
    sentences { speaker_name speaker_id raw_text text }
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


def _drop(query: str, *chunks: str) -> str:
    out = query
    for chunk in chunks:
        out = out.replace(chunk, "")
    return out


def _safe_listing(settings: Settings, limit: int) -> list[dict]:
    try:
        return _post(settings, QUERY, {"limit": limit}).get("transcripts") or []
    except Exception:
        pass
    fallbacks = (
        _drop(QUERY, MEETING_INFO_BLOCK),
        _drop(QUERY, MEETING_INFO_BLOCK, "    meeting_attendees { displayName email name }\n"),
    )
    last_exc: Exception | None = None
    for query in fallbacks:
        try:
            return _post(settings, query, {"limit": limit}).get("transcripts") or []
        except Exception as exc:
            last_exc = exc
    if last_exc:
        raise last_exc
    return []


def _safe_detail(settings: Settings, transcript_id: str, row: dict) -> dict:
    try:
        return _post(settings, DETAIL, {"id": transcript_id}).get("transcript") or row
    except Exception:
        pass
    fallbacks = (
        _drop(DETAIL, SPEAKER_NAME_TOKEN),
        _drop(DETAIL, MEETING_INFO_BLOCK, SPEAKER_NAME_TOKEN),
        _drop(
            DETAIL,
            MEETING_INFO_BLOCK,
            SPEAKER_NAME_TOKEN,
            "    meeting_attendees { displayName email name }\n",
        ),
    )
    last_exc: Exception | None = None
    for query in fallbacks:
        try:
            return _post(settings, query, {"id": transcript_id}).get("transcript") or row
        except Exception as exc:
            last_exc = exc
    if last_exc:
        raise last_exc
    return row


def _format_sentences(sentences: list) -> str:
    lines: list[str] = []
    for raw in sentences or []:
        if not isinstance(raw, dict):
            text = str(raw or "").strip()
            if text:
                lines.append(text)
            continue
        speaker = (raw.get("speaker_name") or raw.get("speaker_id") or "").strip()
        text = (raw.get("text") or raw.get("raw_text") or "").strip()
        if not text:
            continue
        lines.append(f"{speaker}: {text}" if speaker else text)
    return "\n".join(lines)


def _summary_block(summary: dict | None) -> tuple[str, object, object]:
    summary = summary or {}
    overview = summary.get("overview") or ""
    if not isinstance(overview, str):
        overview = str(overview)
    bullets = summary.get("shorthand_bullet")
    items = summary.get("action_items")
    parts: list[str] = []
    if overview.strip():
        parts.append(overview.strip())
    if bullets:
        if isinstance(bullets, list):
            parts.append("\n".join(str(b) for b in bullets if b))
        else:
            parts.append(str(bullets).strip())
    if items:
        if isinstance(items, list):
            parts.append("\n".join(str(i) for i in items if i))
        else:
            parts.append(str(items).strip())
    return "\n\n".join(p for p in parts if p), bullets, items


def _meeting_info(detail: dict, row: dict) -> dict:
    info = detail.get("meeting_info") or row.get("meeting_info") or {}
    return info if isinstance(info, dict) else {}


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
        text = _format_sentences(sentences)[:EXTRACT_TEXT_CAP]
        summary_text, bullets, items = _summary_block(detail.get("summary") or row.get("summary"))
        info = _meeting_info(detail, row)
        silent = bool(info.get("silent_meeting")) or not sentences
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
                summary=summary_text[:EXTRACT_TEXT_CAP],
                raw_subject=title,
                extra={
                    "participants": participants,
                    "meeting_attendees": attendees,
                    "overview": ((detail.get("summary") or {}).get("overview") or ""),
                    "shorthand_bullet": bullets,
                    "action_items": items,
                    "silent_meeting": silent,
                    "summary_status": info.get("summary_status") or "",
                    "sentence_count": len(sentences),
                    "has_sentences": bool(sentences),
                },
            )
        )
    return out
