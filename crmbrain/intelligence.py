from __future__ import annotations

import json
import re
from typing import Any

import requests

from crmbrain.config import STAGE, Settings
from crmbrain.models import Engagement

EXTRACT_PROMPT = """You extract relationship-selling facts for a CRM.

Return ONLY JSON with this shape:
{
  "personal_details": "short paragraph Josh can skim",
  "family_notes": "",
  "relationship_hooks": "",
  "pain_points": "",
  "buying_committee": "",
  "gift_ideas": "",
  "birthday": "YYYY-MM-DD or empty",
  "stage_hint": "discovery_scheduled|discovery_completed|proposal_sent|signed|paid|no_show|nurture|closed_lost|",
  "ticker_reason": "kicked_can|no_show|never_booked|deal_died|",
  "reminders": [{"when": "YYYY-MM-DD", "why": ""}]
}

Rules:
- Only facts the person actually said or that are obvious from the meeting.
- Birthday, kids, spouse, school, sports, city, hobbies matter.
- stage_hint only with clear evidence.
- ticker_reason if they punted, no-showed, or the deal died.
- No dashes in gift_ideas.
"""


def heuristic_extract(text: str) -> dict[str, Any]:
    blob = text or ""
    low = blob.lower()
    facts = {
        "personal_details": "",
        "family_notes": "",
        "relationship_hooks": "",
        "pain_points": "",
        "buying_committee": "",
        "gift_ideas": "",
        "birthday": "",
        "stage_hint": "",
        "ticker_reason": "",
        "reminders": [],
    }
    birthday = re.search(r"\b(?:birthday|born on|bday)\b[^\n.]{0,40}(\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?)", blob, re.I)
    if birthday:
        facts["birthday"] = birthday.group(1)
        facts["personal_details"] = f"Birthday mentioned: {birthday.group(1)}"
        facts["reminders"].append({"when": birthday.group(1), "why": "birthday"})
    kid = re.search(r"\b(son|daughter|kids?|wife|husband|spouse)\b[^\n.]{0,80}", blob, re.I)
    if kid:
        facts["family_notes"] = kid.group(0)
    college = re.search(r"\b([A-Z][A-Za-z]+(?:\s[A-Z][A-Za-z]+){0,3})\s+(University|College)\b", blob)
    if college:
        facts["relationship_hooks"] = college.group(0)
    if any(w in low for w in ("no-show", "no show", "didn't show", "did not show")):
        facts["stage_hint"] = "no_show"
        facts["ticker_reason"] = "no_show"
    if any(w in low for w in ("circle back", "kick the can", "next quarter", "not right now", "reach back out in")):
        facts["ticker_reason"] = facts["ticker_reason"] or "kicked_can"
        facts["stage_hint"] = facts["stage_hint"] or "nurture"
    if any(w in low for w in ("we're going with someone", "deal is dead", "not moving forward", "out of budget")):
        facts["ticker_reason"] = "deal_died"
        facts["stage_hint"] = "closed_lost"
    # Signed / paid / proposal come from Gmail + PandaDoc, not from talk.
    return facts


def extract(settings: Settings, ev: Engagement) -> dict[str, Any]:
    text = "\n".join(x for x in (ev.summary, ev.transcript, ev.raw_subject) if x)[:12000]
    facts = heuristic_extract(text)
    if settings.gemini_key and text.strip():
        try:
            facts = {**facts, **_gemini(settings, text)}
        except Exception:
            pass
    if ev.stage_hint:
        facts["stage_hint"] = facts.get("stage_hint") or ev.stage_hint
    return facts


def _gemini(settings: Settings, text: str) -> dict[str, Any]:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{settings.gemini_model}:generateContent"
    resp = requests.post(
        url,
        params={"key": settings.gemini_key},
        json={
            "contents": [{"parts": [{"text": EXTRACT_PROMPT + "\n\nSOURCE:\n" + text}]}],
            "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
        },
        timeout=45,
    )
    resp.raise_for_status()
    body = resp.json()
    raw = body["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(raw)


def merge_contact_props(existing: dict, facts: dict[str, Any]) -> dict[str, str]:
    def join(old: str, new: str) -> str:
        new = (new or "").strip()
        old = (old or "").strip()
        if not new:
            return old
        if new in old:
            return old
        return (old + "\n" + new).strip() if old else new

    props = existing.get("properties") or {}
    out = {
        "personal_details": join(props.get("personal_details", ""), facts.get("personal_details", "")),
        "family_notes": join(props.get("family_notes", ""), facts.get("family_notes", "")),
        "relationship_hooks": join(props.get("relationship_hooks", ""), facts.get("relationship_hooks", "")),
        "pain_points": join(props.get("pain_points", ""), facts.get("pain_points", "")),
        "buying_committee": join(props.get("buying_committee", ""), facts.get("buying_committee", "")),
        "gift_ideas": join(props.get("gift_ideas", ""), facts.get("gift_ideas", "")),
    }
    if facts.get("birthday") and re.match(r"\d{4}-\d{2}-\d{2}", facts["birthday"]):
        out["date_of_birth"] = facts["birthday"]
    return {k: v for k, v in out.items() if v}


def stage_id(hint: str) -> str:
    if hint in STAGE.values():
        return hint
    return STAGE.get(hint, "")
