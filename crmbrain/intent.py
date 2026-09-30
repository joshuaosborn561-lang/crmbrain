"""Classify whether a meeting / thread is a SalesGlider sales opportunity.

Heuristic first (few-shot cases from Sep 2026 misses). Gemini only when the
heuristic is unsure and a key is configured. Low-confidence cases go to the
review queue — they never open a HubSpot deal.
"""

from __future__ import annotations

import json
import re
from typing import Any

import requests

from crmbrain.config import JOSH_DOMAINS, NON_SALES_TITLE_HINTS, STAGE, Settings, is_client_context
from crmbrain.models import Engagement, IntentDecision

SALES_HINTS = (
    "salesglider",
    "sg intro",
    "discovery",
    "disco call",
    "intro call",
    "cold email",
    "proposal",
    "pricing",
    "agreement",
    "contract",
    "invoice",
    "retainer",
    "poc",
    "proof of concept",
    "pilot",
    "kickoff",
    "onboarding",
    "growth partners",
)
LEARNING_HINTS = (
    "marketing masterclass",
    "masterclass",
    "chorbie",
    "how do you",
    "learn about",
    "asking about marketing",
)
DAY_JOB_HINTS = (
    "meraki",
    "insight.com",
    "dotstech",
    "dots tech",
    "cisco",
    "insight day",
)
PERSONAL_HINTS = (
    "lunch",
    "coffee",
    "catch up",
    "birthday",
    "family",
)
VENDOR_HINTS = (
    "seo partner",
    "vendor",
    "partner sync",
    "seth kingdon",
)
MENTOR_HINTS = (
    "mentor",
    "mark/josh",
    "mark / josh",
    "josh/mark",
    "recurring 1:1",
)
RECRUITER_HINTS = ("recruiter", "recruiting", "talent acquisition")
POC_HINTS = (
    "poc",
    "proof of concept",
    "pilot",
    "kickoff",
    "onboarding",
    "paid poc",
    "paid pilot",
)
KNOWN_NON_SALES_PEOPLE = {
    "cynthia hernandez": "learning",
    "alex branning": "personal",
    "seth kingdon": "vendor",
}

INTENT_PROMPT = """You classify whether a meeting or thread is a SalesGlider SALES opportunity.

SalesGlider is a B2B lead-gen agency that guarantees meetings. Josh sells that service.
A sales opportunity is an EXTERNAL prospect considering buying SalesGlider services,
with a meeting booked/held OR an active proposal/contract/POC/invoice conversation.

NOT a sales opportunity:
- Josh is the buyer, learner, or networker (example: Cynthia Hernandez / Chorbie "Marketing Masterclass")
- Personal/friend meetings (example: Alex Branning, arranged by text)
- Mentors (example: recurring "Mark/Josh" call)
- Vendors/partners (example: Seth Kingdon, SEO partner)
- Lunches, recruiters
- Josh's Insight/Cisco day job (insight.com, DotsTech, "Meraki Discussion")
- Existing clients' internal ops calls (no new commercial paper)

Return ONLY JSON:
{
  "verdict": "yes"|"no"|"review",
  "intent": "sales|buyer|learning|networking|personal|mentor|vendor|recruiter|day_job|client_ops",
  "confidence": 0.0,
  "reason": "one short sentence",
  "stage": "discovery_scheduled|discovery_completed|proposal_sent|signed|paid|no_show|nurture|closed_lost|",
  "amount": ""
}

Rules:
- verdict=yes only when confidently a sales opportunity (confidence >= 0.75).
- verdict=no when confidently not selling SalesGlider.
- verdict=review when unsure. Never invent a deal.
- amount is USD digits only when THIS deal's price is clearly stated. Empty if unsure.
- No free-POC language in reason text.
"""


def _blob(ev: Engagement) -> str:
    extra = ev.extra or {}
    return " ".join(
        str(x)
        for x in (
            ev.raw_subject,
            ev.summary,
            ev.transcript,
            ev.name,
            ev.display_name(),
            ev.company,
            ev.email,
            extra.get("event_type"),
            extra.get("meeting_when"),
            extra.get("document_name"),
        )
        if x
    ).lower()


def _email_domain(email: str) -> str:
    low = (email or "").strip().lower()
    if "@" not in low:
        return ""
    return low.rsplit("@", 1)[-1]


def heuristic_intent(ev: Engagement) -> IntentDecision:
    blob = _blob(ev)
    name = (ev.display_name() or ev.name or "").strip().lower()
    domain = _email_domain(ev.email)

    if domain in JOSH_DOMAINS or domain == "insight.com":
        return IntentDecision(
            verdict="no",
            intent="day_job" if domain == "insight.com" else "personal",
            confidence=0.95,
            reason="Josh domain — not an external prospect",
        )

    for person, intent in KNOWN_NON_SALES_PEOPLE.items():
        if person in name or person in blob:
            return IntentDecision(
                verdict="no",
                intent=intent,
                confidence=0.93,
                reason=f"Known non-opportunity: {person}",
            )

    if any(h in blob for h in DAY_JOB_HINTS):
        return IntentDecision(
            verdict="no",
            intent="day_job",
            confidence=0.92,
            reason="Insight/Cisco/Meraki day-job context",
        )
    if any(h in blob for h in LEARNING_HINTS):
        return IntentDecision(
            verdict="no",
            intent="learning",
            confidence=0.9,
            reason="Josh is learning, not selling",
        )
    if any(h in blob for h in MENTOR_HINTS):
        return IntentDecision(
            verdict="no",
            intent="mentor",
            confidence=0.9,
            reason="Mentor / recurring Mark-Josh style call",
        )
    if any(h in blob for h in VENDOR_HINTS):
        return IntentDecision(
            verdict="no",
            intent="vendor",
            confidence=0.9,
            reason="Vendor or partner, not a prospect",
        )
    if any(h in blob for h in RECRUITER_HINTS):
        return IntentDecision(
            verdict="no",
            intent="recruiter",
            confidence=0.9,
            reason="Recruiter meeting",
        )
    if any(h in blob for h in PERSONAL_HINTS) and not any(h in blob for h in SALES_HINTS):
        return IntentDecision(
            verdict="no",
            intent="personal",
            confidence=0.86,
            reason="Personal/social meeting with no sales language",
        )
    if any(h in blob for h in NON_SALES_TITLE_HINTS) and not any(h in blob for h in SALES_HINTS):
        return IntentDecision(
            verdict="no",
            intent="networking",
            confidence=0.84,
            reason="Title matches a non-sales pattern",
        )

    if is_client_context(ev.display_name(), ev.company, ev.raw_subject) and not any(
        h in blob for h in ("proposal", "agreement", "invoice", "pandadoc", "docusign", "paid", "growth partners")
    ):
        return IntentDecision(
            verdict="no",
            intent="client_ops",
            confidence=0.8,
            reason="Existing client ops — notes only unless commercial paper",
        )

    sales_hit = next((h for h in SALES_HINTS if h in blob), "")
    if sales_hit:
        stage = ""
        if any(h in blob for h in POC_HINTS):
            stage = STAGE["signed"]
        elif ev.source in {"fireflies", "cube_acr", "allo"}:
            stage = STAGE["discovery_completed"]
        elif ev.source in {"calendly", "gmail"} or ev.stage_hint in {
            STAGE["discovery_scheduled"],
            "qualifiedtobuy",
            "discovery_scheduled",
        }:
            stage = STAGE["discovery_scheduled"]
        return IntentDecision(
            verdict="yes",
            intent="sales",
            confidence=0.9,
            reason=f"Sales evidence ({sales_hit})",
            stage=stage,
        )

    if ev.source in {"smartlead", "heyreach", "rvm", "gmail_person"}:
        return IntentDecision(
            verdict="no",
            intent="networking",
            confidence=0.88,
            reason="Reply/chat/RVM alone is not a booked meeting",
        )

    return IntentDecision(
        verdict="review",
        intent="",
        confidence=0.4,
        reason="Not enough signal to call this a sales opportunity",
    )


def classify(settings: Settings | None, ev: Engagement) -> IntentDecision:
    decision = heuristic_intent(ev)
    if decision.verdict != "review":
        return decision
    if not settings or not settings.gemini_key:
        return decision
    text = _blob(ev)[:8000]
    if not text.strip():
        return decision
    try:
        model = _gemini_intent(settings, text)
    except Exception:
        return decision
    return _merge_model(decision, model, settings.intent_min_confidence)


def is_confident_sales(decision: IntentDecision, min_confidence: float = 0.75) -> bool:
    return decision.verdict == "yes" and decision.confidence >= min_confidence


def is_confident_non_sales(decision: IntentDecision, min_confidence: float = 0.75) -> bool:
    return decision.verdict == "no" and decision.confidence >= min_confidence


def _merge_model(base: IntentDecision, incoming: dict[str, Any], min_confidence: float) -> IntentDecision:
    verdict = str(incoming.get("verdict") or base.verdict).strip().lower()
    if verdict not in {"yes", "no", "review"}:
        verdict = base.verdict
    try:
        confidence = float(incoming.get("confidence") if incoming.get("confidence") is not None else base.confidence)
    except (TypeError, ValueError):
        confidence = base.confidence
    confidence = max(0.0, min(1.0, confidence))
    if verdict == "yes" and confidence < min_confidence:
        verdict = "review"
    amount = str(incoming.get("amount") or "").strip()
    if amount and not re.fullmatch(r"\d+(?:\.\d+)?", amount):
        amount = ""
    return IntentDecision(
        verdict=verdict,
        intent=str(incoming.get("intent") or base.intent).strip(),
        confidence=confidence,
        reason=str(incoming.get("reason") or base.reason).strip(),
        stage=str(incoming.get("stage") or base.stage).strip(),
        amount=amount or base.amount,
    )


def _gemini_intent(settings: Settings, text: str) -> dict[str, Any]:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{settings.gemini_model}:generateContent"
    resp = requests.post(
        url,
        params={"key": settings.gemini_key},
        json={
            "contents": [{"parts": [{"text": INTENT_PROMPT + "\n\nSOURCE:\n" + text}]}],
            "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"},
        },
        timeout=45,
    )
    resp.raise_for_status()
    body = resp.json()
    raw = body["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(raw)
