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
from crmbrain.policy import (
    CONFIDENT_NO_INTENTS,
    NEVER_OPEN_DEAL_SOURCES,
    STRICT_DISCOVERY_HINTS,
    has_word_hint,
    is_closed_won_client,
)

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
# Josh-is-the-student only. "how do you" / "learn about" are discovery talk.
LEARNING_HINTS = (
    "marketing masterclass",
    "masterclass",
    "chorbie",
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
# Josh's supplier. Bare "vendor" in a Cube transcript is prospect language.
VENDOR_HINTS = (
    "seo partner",
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
# Josh is the employer/buyer only. Bare "hiring" / "cold caller" / "paid trial"
# are ICP language (staffing firms, outbound prospects) and must not fire.
HIRE_HINTS = (
    "contractor agreement",
    "contractor-agreement",
    "i'm hiring you",
    "i am hiring you",
    "josh is hiring you",
    "josh is hiring",
    "we'd pay you",
    "we would pay you",
    "i'd pay you",
    "i would pay you",
    "your trial with salesglider as a caller",
    "trial with salesglider as a caller",
)
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
- Josh is hiring or contracting ONLY when Josh is the employer/buyer (e.g. "I'm hiring you", "we'd pay you", contractor agreement, "your trial with SalesGlider as a caller"). Intent = hire.
- A prospect saying they are hiring staff, or that their cold caller / SDR is not working, is still a sales opportunity. Never mark those hire.
- Personal/friend meetings (example: Alex Branning, arranged by text)
- Mentors (example: recurring "Mark/Josh" call)
- Vendors/partners (example: Seth Kingdon, SEO partner)
- Lunches, recruiters
- Josh's Insight/Cisco day job (insight.com, DotsTech, "Meraki Discussion")
- Existing clients' internal ops calls (no new commercial paper)

Do NOT mark learning because someone said "how do you" or "learn about" — those are normal discovery questions. learning = Josh is the student (Chorbie / Marketing Masterclass only).
Do NOT mark vendor because the word "vendor" appears in a sales call (prospects talk about their vendors). vendor = Josh's supplier (Seth Kingdon / SEO partner) only.

Return ONLY JSON:
{
  "verdict": "yes"|"no"|"review",
  "intent": "sales|buyer|learning|networking|personal|mentor|vendor|recruiter|day_job|client_ops|hire|contractor",
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
- hire/contractor only when Josh is clearly the employer or buyer. Never from "we're hiring" or "cold caller" alone.
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


def has_sales_context(ev: Engagement) -> bool:
    """True when the thread is a SalesGlider opportunity, not Josh learning/buying."""
    blob = _blob(ev)
    if has_word_hint(blob, SALES_HINTS) or has_word_hint(blob, STRICT_DISCOVERY_HINTS):
        return True
    extra = ev.extra or {}
    if extra.get("document_id") or extra.get("document_name") or extra.get("create_new"):
        return True
    if ev.stage_hint in {
        STAGE["signed"],
        STAGE["paid"],
        STAGE["proposal_sent"],
        STAGE["discovery_scheduled"],
        STAGE["discovery_completed"],
    }:
        return True
    if ev.source == "calendly":
        return True
    return False


def _veto_learning_vendor_in_sales_context(ev: Engagement, decision: IntentDecision) -> IntentDecision:
    if decision.verdict != "no" or (decision.intent or "") not in {"learning", "vendor"}:
        return decision
    if not has_sales_context(ev):
        return decision
    name = (ev.display_name() or ev.name or "").strip().lower()
    blob = _blob(ev)
    for person, _intent in KNOWN_NON_SALES_PEOPLE.items():
        if person in name or person in blob:
            return decision
    return IntentDecision(
        verdict="review",
        intent="",
        confidence=min(decision.confidence, 0.4),
        reason="Sales context — not learning/vendor",
        stage=decision.stage,
        amount=decision.amount,
        via=decision.via,
    )


def _email_domain(email: str) -> str:
    low = (email or "").strip().lower()
    if "@" not in low:
        return ""
    return low.rsplit("@", 1)[-1]


def _is_plain_gmail(ev: Engagement) -> bool:
    if ev.source == "gmail_person":
        return True
    if ev.source != "gmail":
        return False
    extra = ev.extra or {}
    if extra.get("create_new") or extra.get("gcal_create"):
        return False
    blob = _blob(ev)
    if any(h in blob for h in ("calendly", "pandadoc", "docusign", "calendar-notification", "stripe.com")):
        return False
    return True


def _has_salesglider_deal(ev: Engagement) -> bool:
    extra = ev.extra or {}
    return bool(extra.get("has_sg_deal") or extra.get("closed_won") or extra.get("already_prospect"))


def _explicit_hire_evidence(blob: str) -> str:
    return next((h for h in HIRE_HINTS if h in blob), "")


def heuristic_intent(ev: Engagement) -> IntentDecision:
    blob = _blob(ev)
    name = (ev.display_name() or ev.name or "").strip().lower()
    domain = _email_domain(ev.email)
    deal_holder = _has_salesglider_deal(ev)

    if ev.source in NEVER_OPEN_DEAL_SOURCES or _is_plain_gmail(ev):
        return IntentDecision(
            verdict="no",
            intent="networking",
            confidence=0.88,
            reason="Reply/chat/RVM/plain Gmail alone is not a booked meeting",
        )

    if (domain in JOSH_DOMAINS or domain == "insight.com") and not deal_holder:
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

    if any(h in blob for h in DAY_JOB_HINTS) and not deal_holder:
        return IntentDecision(
            verdict="no",
            intent="day_job",
            confidence=0.92,
            reason="Insight/Cisco/Meraki day-job context",
        )
    if any(h in blob for h in LEARNING_HINTS) and not has_sales_context(ev):
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
    if any(h in blob for h in VENDOR_HINTS) and not has_sales_context(ev):
        return IntentDecision(
            verdict="no",
            intent="vendor",
            confidence=0.9,
            reason="Vendor or partner, not a prospect",
        )
    hire_hit = _explicit_hire_evidence(blob)
    if hire_hit:
        return IntentDecision(
            verdict="no",
            intent="hire",
            confidence=0.93,
            reason="Josh is hiring or contracting, not selling",
        )
    if any(h in blob for h in RECRUITER_HINTS) and not deal_holder:
        return IntentDecision(
            verdict="no",
            intent="recruiter",
            confidence=0.9,
            reason="Recruiter meeting",
        )
    if any(h in blob for h in PERSONAL_HINTS) and not any(h in blob for h in SALES_HINTS) and not deal_holder:
        return IntentDecision(
            verdict="no",
            intent="personal",
            confidence=0.86,
            reason="Personal/social meeting with no sales language",
        )
    if (
        any(h in blob for h in NON_SALES_TITLE_HINTS)
        and not any(h in blob for h in SALES_HINTS)
        and not deal_holder
    ):
        return IntentDecision(
            verdict="no",
            intent="networking",
            confidence=0.84,
            reason="Title matches a non-sales pattern",
        )

    if (
        is_client_context(ev.display_name(), ev.company, ev.raw_subject)
        and is_closed_won_client(ev)
        and not any(
            h in blob for h in ("proposal", "agreement", "invoice", "pandadoc", "docusign", "paid", "growth partners")
        )
    ):
        return IntentDecision(
            verdict="no",
            intent="client_ops",
            confidence=0.8,
            reason="Existing client ops — notes only unless commercial paper",
        )

    if ev.source in {"cube_acr", "fireflies"}:
        sales_hit = has_word_hint(blob, STRICT_DISCOVERY_HINTS)
        if not sales_hit:
            return IntentDecision(
                verdict="review",
                intent="",
                confidence=0.4,
                reason="Held call needs a discovery hint or Gemini yes",
            )
    else:
        sales_hit = has_word_hint(blob, SALES_HINTS)
    if sales_hit:
        stage = ""
        if ev.source in {"fireflies", "cube_acr", "allo"}:
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

    return IntentDecision(
        verdict="review",
        intent="",
        confidence=0.4,
        reason="Not enough signal to call this a sales opportunity",
    )


def apply_deal_holder_veto(ev: Engagement, decision: IntentDecision | None = None) -> IntentDecision | None:
    """Open/recent SalesGlider deal holders are never day_job/hire/recruiter without explicit hire."""
    decision = decision or getattr(ev, "_intent_decision", None)
    if not isinstance(decision, IntentDecision):
        return decision
    if not _has_salesglider_deal(ev):
        return decision
    if (decision.intent or "") not in {"day_job", "hire", "recruiter", "networking"}:
        return decision
    if decision.intent == "hire" and _explicit_hire_evidence(_blob(ev)):
        return decision
    if is_client_context(ev.display_name(), ev.company, ev.raw_subject) and is_closed_won_client(ev):
        rewritten = IntentDecision(
            verdict="no",
            intent="client_ops",
            confidence=max(decision.confidence, 0.8),
            reason="Paid/Signed client — notes only, not day-job/hire",
            stage=decision.stage,
            amount=decision.amount,
            via=decision.via,
        )
    else:
        rewritten = IntentDecision(
            verdict="yes",
            intent="sales",
            confidence=max(0.8, min(decision.confidence, 0.9)),
            reason="Open SalesGlider deal — stay updatable, not day-job/hire/recruiter",
            stage=decision.stage,
            amount=decision.amount,
            via=decision.via,
        )
    ev._intent_decision = rewritten
    ev._person_intent = rewritten
    extra = ev.extra
    extra["intent_no"] = is_confident_non_sales(rewritten, 0.75)
    extra["intent_gemini_yes"] = rewritten.via == "gemini" and is_confident_sales(rewritten, 0.75)
    return normalize_client_ops(ev, rewritten)


def normalize_client_ops(ev: Engagement, decision: IntentDecision | None) -> IntentDecision | None:
    """client_ops is Paid/Signed only. Open pipeline stays sales or review."""
    if not isinstance(decision, IntentDecision):
        return decision
    if (decision.intent or "") != "client_ops":
        return decision
    if is_closed_won_client(ev):
        return decision
    blob = _blob(ev)
    if ev.source in {"cube_acr", "fireflies"}:
        sales_hit = has_word_hint(blob, STRICT_DISCOVERY_HINTS) or has_word_hint(blob, SALES_HINTS)
    else:
        sales_hit = has_word_hint(blob, SALES_HINTS)
    if sales_hit or _has_salesglider_deal(ev):
        rewritten = IntentDecision(
            verdict="yes",
            intent="sales",
            confidence=max(0.8, min(decision.confidence or 0.8, 0.9)),
            reason="Open deal — not a Paid/Signed client, stay updatable",
            stage=decision.stage,
            amount=decision.amount,
            via=decision.via,
        )
    else:
        rewritten = IntentDecision(
            verdict="review",
            intent="",
            confidence=min(decision.confidence or 0.4, 0.4),
            reason="client_ops without Paid/Signed",
            stage="",
            amount=decision.amount,
            via=decision.via,
        )
    ev._intent_decision = rewritten
    ev._person_intent = rewritten
    extra = ev.extra
    extra["intent_no"] = is_confident_non_sales(rewritten, 0.75)
    extra["intent_gemini_yes"] = rewritten.via == "gemini" and is_confident_sales(rewritten, 0.75)
    return rewritten


def classify(settings: Settings | None, ev: Engagement) -> IntentDecision:
    cached = getattr(ev, "_intent_decision", None)
    if isinstance(cached, IntentDecision):
        vetoed = apply_deal_holder_veto(ev, cached) or cached
        return normalize_client_ops(ev, vetoed) or vetoed
    decision = heuristic_intent(ev)
    decision = _veto_learning_vendor_in_sales_context(ev, decision)
    if decision.verdict == "review" and settings and settings.gemini_key:
        text = _blob(ev)[:8000]
        if text.strip():
            try:
                model = _gemini_intent(settings, text)
                decision = _merge_model(decision, model, settings.intent_min_confidence)
                decision.via = "gemini"
                decision = _veto_learning_vendor_in_sales_context(ev, decision)
            except Exception:
                pass
    decision = apply_deal_holder_veto(ev, decision) or decision
    decision = normalize_client_ops(ev, decision) or decision
    ev._intent_decision = decision
    extra = ev.extra
    extra["intent_via"] = decision.via
    extra["intent_gemini_yes"] = decision.via == "gemini" and is_confident_sales(
        decision, getattr(settings, "intent_min_confidence", 0.75) if settings else 0.75
    )
    extra["intent_no"] = is_confident_non_sales(
        decision, getattr(settings, "intent_min_confidence", 0.75) if settings else 0.75
    )
    return decision


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
        via="gemini",
    )


def _gemini_intent(settings: Settings, text: str) -> dict[str, Any]:
    from crmbrain.config import resolve_gemini_model

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{resolve_gemini_model(getattr(settings, 'gemini_model', ''))}:generateContent"
    )
    resp = requests.post(
        url,
        params={"key": settings.gemini_key},
        json={
            "contents": [{"parts": [{"text": INTENT_PROMPT + "\n\nSOURCE:\n" + text}]}],
            "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"},
        },
        timeout=45,
    )
    try:
        resp.raise_for_status()
    except Exception as exc:
        from crmbrain.config import redact_secrets

        raise RuntimeError(redact_secrets(str(exc))) from None
    body = resp.json()
    raw = body["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(raw)


_SOURCE_RANK = {
    "fireflies": 5,
    "cube_acr": 5,
    "calendly": 4,
    "gmail": 3,
    "allo": 2,
}


def _person_key(ev: Engagement) -> str:
    from crmbrain.evidence import person_key

    return person_key(ev.email, ev.phone, ev.display_name() or ev.name)


def is_confident_no_intent(decision: IntentDecision, min_confidence: float = 0.75) -> bool:
    return is_confident_non_sales(decision, min_confidence) and (decision.intent or "") in CONFIDENT_NO_INTENTS


def _cohort(ev: Engagement) -> list[Engagement]:
    group = getattr(ev, "_person_events", None)
    if group:
        return list(group)
    return [ev]


def is_calendly_booking(ev: Engagement) -> bool:
    extra = ev.extra or {}
    if ev.source == "calendly":
        return True
    if extra.get("create_new") and extra.get("event_type"):
        return True
    return "calendly" in f"{ev.raw_subject} {extra.get('from') or ''}".lower()


# Only Josh-as-employer nos may still apply a real client close.
COMMERCE_OVERRIDE_INTENTS = frozenset({"hire", "recruiter"})


def has_client_commerce(ev: Engagement) -> bool:
    """Completed non-free client paper or a payment — never swallowed by hire no."""
    from crmbrain.documents import is_payment_mail, looks_free_document, looks_josh_pays_document

    extra = ev.extra or {}
    doc_name = str(extra.get("document_name") or "")
    subject = ev.raw_subject or ""
    body = ev.summary or ""
    if looks_josh_pays_document(subject, body, doc_name):
        return False
    if ev.stage_hint in {STAGE["signed"], STAGE["paid"]}:
        if looks_free_document(subject, body, doc_name):
            return False
        return True
    if extra.get("document_id") or doc_name:
        if looks_free_document(subject, body, doc_name):
            return False
        return True
    sender = str(extra.get("from") or "")
    if is_payment_mail(subject, sender, body):
        return True
    blob = f"{subject} {body}".lower()
    return "you received a payment" in blob or "payment received" in blob


def person_has_signed_or_payment(ev: Engagement) -> bool:
    """Real close evidence only — not a document mention or mentor/vendor paper."""
    from crmbrain.documents import looks_free_document, looks_josh_pays_document
    from crmbrain.evidence import KIND_PAYMENT, KIND_SIGNED, kind_for

    for item in _cohort(ev):
        extra = item.extra or {}
        doc_name = str(extra.get("document_name") or "")
        subject = item.raw_subject or ""
        body = item.summary or ""
        if looks_josh_pays_document(subject, body, doc_name):
            continue
        if looks_free_document(subject, body, doc_name):
            continue
        if kind_for(item) in {KIND_SIGNED, KIND_PAYMENT}:
            return True
    return False


def person_has_client_commerce(ev: Engagement) -> bool:
    return any(has_client_commerce(item) for item in _cohort(ev))


def commerce_overrides_person_no(ev: Engagement, settings: Settings | None = None) -> bool:
    """Hire/recruiter no yields only to KIND_SIGNED or KIND_PAYMENT."""
    min_c = getattr(settings, "intent_min_confidence", 0.75) if settings else 0.75
    decision = getattr(ev, "_person_intent", None)
    if not isinstance(decision, IntentDecision) or not is_confident_no_intent(decision, min_c):
        return False
    if (decision.intent or "") not in COMMERCE_OVERRIDE_INTENTS:
        return False
    return person_has_signed_or_payment(ev)


def person_has_booking_or_commerce(ev: Engagement) -> bool:
    return any(is_calendly_booking(item) or has_client_commerce(item) for item in _cohort(ev))


def person_blocks_deal(ev: Engagement, settings: Settings | None = None) -> bool:
    """Confident listed no, unless hire/recruiter plus a real client close."""
    min_c = getattr(settings, "intent_min_confidence", 0.75) if settings else 0.75
    decision = getattr(ev, "_person_intent", None)
    if not isinstance(decision, IntentDecision) or not is_confident_no_intent(decision, min_c):
        return False
    if commerce_overrides_person_no(ev, settings):
        return False
    return True


def attach_timeline_intent(settings: Settings | None, engagements: list[Engagement] | None) -> Engagement | None:
    """Classify the person from every engagement on a reconcile timeline."""
    evs = [ev for ev in (engagements or []) if ev]
    if not evs:
        return None
    attach_person_intent(settings, evs)
    return evs[0]


def person_blocks_engagements(
    settings: Settings | None, engagements: list[Engagement] | None
) -> bool:
    ev = attach_timeline_intent(settings, engagements)
    if ev is None:
        return False
    return person_blocks_deal(ev, settings)


def _merged_engagement(events: list[Engagement]) -> Engagement:
    ranked = sorted(events, key=lambda e: _SOURCE_RANK.get(e.source, 0), reverse=True)
    primary = ranked[0]
    subjects = [e.raw_subject for e in events if e.raw_subject]
    transcripts = [e.transcript for e in events if e.transcript]
    summaries = [e.summary for e in events if e.summary]
    extra: dict[str, Any] = {}
    for ev in events:
        extra.update(ev.extra or {})
    email = next((e.email for e in events if e.email), "")
    phone = next((e.phone for e in events if e.phone), "")
    first = next((e.first_name for e in events if e.first_name), "")
    last = next((e.last_name for e in events if e.last_name), "")
    name = next((e.name for e in events if e.name), "")
    company = next((e.company for e in events if e.company), "")
    return Engagement(
        source=primary.source,
        external_id=f"merged:{primary.external_id}",
        occurred_at=primary.occurred_at,
        first_name=first,
        last_name=last,
        name=name,
        email=email,
        phone=phone,
        company=company,
        raw_subject=" ".join(subjects),
        summary=" ".join(summaries),
        transcript="\n".join(transcripts),
        extra=extra,
    )


def attach_person_intent(settings: Settings | None, events: list[Engagement]) -> dict[str, IntentDecision]:
    """Classify each person from all cycle evidence. A confident listed 'no' wins.

    Reuses a decision already attached on the cohort so Gemini is not called again.
    """
    groups: dict[str, list[Engagement]] = {}
    for ev in events or []:
        key = _person_key(ev)
        if not key:
            continue
        groups.setdefault(key, []).append(ev)
    min_c = getattr(settings, "intent_min_confidence", 0.75) if settings else 0.75
    out: dict[str, IntentDecision] = {}
    for key, evs in groups.items():
        cached = [getattr(ev, "_person_intent", None) for ev in evs]
        if cached and all(isinstance(item, IntentDecision) for item in cached):
            winner = apply_deal_holder_veto(evs[0], cached[0]) or cached[0]
            out[key] = winner
            for ev in evs:
                ev._person_intent = winner
                ev._person_events = evs
            continue
        winner = None
        for ev in evs:
            decision = classify(settings, ev)
            if is_confident_no_intent(decision, min_c):
                winner = decision
                break
        if winner is None and len(evs) > 1:
            winner = classify(settings, _merged_engagement(evs))
        if winner is None:
            winner = classify(settings, evs[0])
        winner = apply_deal_holder_veto(evs[0], winner) or winner
        out[key] = winner
        for ev in evs:
            ev._person_intent = winner
            ev._person_events = evs
            ev._intent_decision = getattr(ev, "_intent_decision", None) or winner
            apply_deal_holder_veto(ev, getattr(ev, "_intent_decision", winner))
    return out
