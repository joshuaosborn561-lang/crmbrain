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
  "amount_hint": "",
  "deal_amount": "",
  "reminders": [{"when": "YYYY-MM-DD", "why": ""}]
}

Rules:
- Only facts the person actually said or that are obvious from the meeting.
- Birthday, kids, spouse, school, sports, city, hobbies matter.
- stage_hint only with clear evidence.
- ticker_reason if they punted, no-showed, or the deal died.
- amount_hint / deal_amount: USD number only when THIS deal's price was clearly stated
  (monthly retainer, proposal dollar amount, package). Examples: "3000", "8500".
  Empty if unsure. Never invent. Never use Josh's case-study stats
  ($2M pipeline, $100K closed, free 10K lead campaign).
- No dashes in gift_ideas.
"""

# Case-study / pitch language — never treat these as deal value.
_PITCH_HINTS = (
    "pipeline",
    "first 3 months",
    "first three months",
    "lead campaign",
    "10k lead",
    "free 10k",
    "replies per month",
    "airpods",
    "case study",
    "across our",
    "one of our",
)
_PRICE_HINTS = (
    "retainer",
    "per month",
    "/mo",
    "a month",
    "each month",
    "monthly",
    "package",
    "proposal",
    "quoted",
    "quote",
    "our fee",
    "the fee",
    "investment",
    "pricing",
    "price",
    "one-time",
    "one time",
    "upfront",
    "invoice",
    "would be",
    "that's $",
    "thats $",
    "pay",
    "cost us",
    "cost is",
)
_MONEY_RE = re.compile(
    r"\$\s*(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*([kKmM])?"
    r"|(\d{1,3}(?:,\d{3})+)\s*([kKmM])?"
    r"|(\d+(?:\.\d+)?)\s*([kK])\b"
)


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
        "amount_hint": "",
        "deal_amount": "",
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
    amount = parse_deal_amount(blob)
    facts["amount_hint"] = amount
    facts["deal_amount"] = amount
    # Signed / paid / proposal stages come from Gmail + PandaDoc, not from talk.
    return facts


def _money_value(num: str, suffix: str) -> float | None:
    try:
        val = float(num.replace(",", ""))
    except ValueError:
        return None
    suf = (suffix or "").lower()
    if suf == "k":
        val *= 1000
    elif suf == "m":
        val *= 1_000_000
    return val


def format_amount(val: float) -> str:
    if val < 50 or val > 500_000:
        return ""
    if abs(val - round(val)) < 0.001:
        return str(int(round(val)))
    return f"{val:.2f}".rstrip("0").rstrip(".")


def _window_has(text: str, needles: tuple[str, ...]) -> bool:
    return any(n in text for n in needles)


def parse_deal_amount(text: str) -> str:
    """USD monthly or one-time when clearly stated. Never invent."""
    if not text:
        return ""
    hits: list[str] = []
    for match in _MONEY_RE.finditer(text):
        num = match.group(1) or match.group(3) or match.group(5)
        suffix = match.group(2) or match.group(4) or match.group(6) or ""
        val = _money_value(num, suffix)
        if val is None:
            continue
        start = max(0, match.start() - 48)
        end = min(len(text), match.end() + 48)
        window = text[start:end].lower()
        if _window_has(window, _PITCH_HINTS):
            continue
        if not _window_has(window, _PRICE_HINTS) and "$" not in match.group(0):
            continue
        if not _window_has(window, _PRICE_HINTS):
            # Bare $5,000 with no retainer/proposal/package context is not enough.
            continue
        formatted = format_amount(val)
        if formatted:
            hits.append(formatted)
    unique = list(dict.fromkeys(hits))
    if len(unique) == 1:
        return unique[0]
    return ""


def amount_attested_in_text(text: str, amount: str) -> bool:
    if not text or not amount:
        return False
    compact = text.replace(",", "")
    try:
        n = int(float(amount))
    except ValueError:
        return False
    if re.search(rf"\$?\s*{n}(?:\.0+)?\b", compact):
        return True
    if n >= 1000 and n % 1000 == 0 and re.search(rf"\$?\s*{n // 1000}\s*k\b", compact, re.I):
        return True
    return False


def normalize_amount_hint(hint: object, text: str) -> str:
    """Keep a model/heuristic amount only when the transcript attests it."""
    heuristic = parse_deal_amount(text)
    raw = str(hint or "").strip()
    if not raw:
        return heuristic
    parsed = parse_deal_amount(raw) or parse_deal_amount(f"${raw}")
    if not parsed:
        cleaned = re.sub(r"[^\d.]", "", raw)
        try:
            parsed = format_amount(float(cleaned))
        except ValueError:
            parsed = ""
    if parsed and amount_attested_in_text(text, parsed):
        return parsed
    return heuristic


def amount_to_write(current_amount: object, hint: str) -> str:
    """Fill HubSpot amount only when the deal amount is empty."""
    if not hint:
        return ""
    cur = str(current_amount or "").strip()
    if cur and cur not in {"0", "0.0", "0.00"}:
        return ""
    return hint


def merge_fact_dicts(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Gemini empty strings must not wipe heuristic facts."""
    out = dict(base)
    for key, value in (incoming or {}).items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if value in ([], {}):
            continue
        out[key] = value
    return out


def extract(settings: Settings, ev: Engagement) -> dict[str, Any]:
    text = "\n".join(x for x in (ev.summary, ev.transcript, ev.raw_subject) if x)[:12000]
    facts = heuristic_extract(text)
    if settings.gemini_key and text.strip():
        try:
            facts = merge_fact_dicts(facts, _gemini(settings, text))
        except Exception:
            pass
    if ev.stage_hint:
        facts["stage_hint"] = facts.get("stage_hint") or ev.stage_hint
    amount = normalize_amount_hint(facts.get("amount_hint") or facts.get("deal_amount"), text)
    facts["amount_hint"] = amount
    facts["deal_amount"] = amount
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
