"""Josh's HubSpot keep rules: meeting-held/scheduled only, honest stages."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from crmbrain.config import (
    JOSH_DOMAINS,
    JOSH_EMAILS,
    STAGE,
    Settings,
    has_not_deal_note,
    is_client_context,
    is_excluded_contact,
    is_zoom_room_address,
    now_utc,
)
from crmbrain.intelligence import stage_id
from crmbrain.models import Engagement
from crmbrain.names import (
    format_deal_name,
    is_confident_person_name,
    is_weak_deal_name,
    looks_like_meeting_title,
    parse_attendee_token,
    prefer_deal_name,
)

# Sources that may CREATE a HubSpot contact (meeting booked or held).
HUBSPOT_CREATE_SOURCES = frozenset({"fireflies", "calendly", "cube_acr", "allo"})
# These never open a deal and never create a contact. Ticker is fine.
NEVER_OPEN_DEAL_SOURCES = frozenset({"smartlead", "heyreach", "rvm", "gmail_person"})
TICKER_WITHOUT_HUBSPOT = frozenset({"smartlead", "heyreach", "rvm"})
MEETING_CRM_SOURCES = frozenset({"fireflies", "calendly", "cube_acr", "allo"})
NO_SHOW_GRACE = timedelta(hours=2)
HELD_MATCH_WINDOW = timedelta(hours=24)
FREE_MAIL_DOMAINS = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "yahoo.com",
        "outlook.com",
        "hotmail.com",
        "live.com",
        "icloud.com",
        "me.com",
        "aol.com",
        "proton.me",
        "protonmail.com",
        "msn.com",
    }
)
JOSH_NAME_KEYS = frozenset({"joshua osborn", "josh osborn", "joshua", "josh"})
_NOTETAKER_DOMAINS = frozenset(
    {"fireflies.ai", "otter.ai", "fathom.video", "read.ai", "krisp.ai", "tldv.io"}
)

DISCOVERY_HINTS = (
    "salesglider intro",
    "sg intro",
    "discovery",
    "disco call",
    "disco ",
    "intro call",
    "salesglider",
)
# Cube/Fireflies deal-create: word-boundary only. No generic campaign/leads/roof.
STRICT_DISCOVERY_HINTS = (
    "intro",
    "discovery",
    "pricing",
    "proposal",
    "contract",
    "retainer",
)
SALESGLIDER_INTRO_HINTS = ("salesglider intro", "sg intro")
FAMILY_ONLY_HINTS = (
    "love you",
    "pick up the kids",
    "soccer practice",
    "what's for dinner",
    "what’s for dinner",
    "family dinner",
)
BUSINESS_HINTS = (
    "salesglider",
    "pipeline",
    "proposal",
    "leads",
    "campaign",
    "owner",
    "roof",
    "hvac",
    "discovery",
    "budget",
    "roi",
)

# Higher = more advanced open pipeline. Replied/Nurture are weak.
STAGE_RANK = {
    STAGE["closed_lost"]: 0,
    STAGE["replied"]: 1,
    STAGE["nurture"]: 1,
    STAGE["no_show"]: 2,
    STAGE["discovery_scheduled"]: 3,
    STAGE["discovery_completed"]: 4,
    STAGE["proposal_sent"]: 5,
    STAGE["signed"]: 6,
    STAGE["paid"]: 7,
}
BACK_STAGES = {STAGE["nurture"], STAGE["no_show"], STAGE["closed_lost"]}
WEAK_STAGES = {STAGE["replied"], STAGE["nurture"]}
MONEY_STAGES = {STAGE["signed"], STAGE["paid"], STAGE["proposal_sent"]}
MEETING_STAGES = {
    STAGE["discovery_scheduled"],
    STAGE["discovery_completed"],
    STAGE["proposal_sent"],
    STAGE["signed"],
    STAGE["paid"],
}
PRE_SALE_STAGES = {
    STAGE["replied"],
    STAGE["discovery_scheduled"],
    STAGE["discovery_completed"],
}
CLOSED_WON_STAGES = {STAGE["signed"], STAGE["paid"]}
DEFAULT_PIPELINE = "default"
COMMERCE_AMOUNT_PROPS = ("hs_mrr", "hs_arr", "hs_acv", "hs_tcv")
COMMERCE_DEALTYPES = {"subscription", "recurring", "commerce"}


def _blob(ev: Engagement) -> str:
    extra = ev.extra or {}
    return " ".join(
        str(x)
        for x in (
            ev.raw_subject,
            ev.summary,
            ev.name,
            extra.get("event_type"),
            extra.get("meeting_when"),
        )
        if x
    ).lower()


def looks_like_html(text: str) -> bool:
    low = (text or "").lstrip()[:400].lower()
    return low.startswith("<!doctype") or low.startswith("<html") or "<html" in low


def is_client_context_ev(ev: Engagement) -> bool:
    return is_client_context(ev.display_name(), ev.company, ev.raw_subject)


def is_silent_meeting(ev: Engagement) -> bool:
    """Fireflies silent_meeting / no sentences is not a held call."""
    extra = ev.extra or {}
    if extra.get("silent_meeting") is True:
        return True
    status = str(extra.get("summary_status") or "").lower()
    if "silent" in status:
        return True
    if extra.get("sentence_count") == 0 or extra.get("has_sentences") is False:
        return True
    return False


CALL_SCREENER_HINTS = (
    "call screen",
    "call screener",
    "google call screen",
    "this call is being screened",
    "please state your name and reason",
    "the person you are calling is using",
    "unknown caller screening",
    "you've reached the google call screen",
    "hi, you've reached",
)


def is_call_screener(ev: Engagement) -> bool:
    """Voicemail / Google Call Screen / Silence Unknown Callers is not a held meeting."""
    extra = ev.extra or {}
    if extra.get("call_screener") is True or extra.get("screener") is True:
        return True
    blob = f"{ev.raw_subject or ''} {ev.summary or ''} {ev.transcript or ''}".lower()
    return any(h in blob for h in CALL_SCREENER_HINTS)


def is_closed_won_client(ev: Engagement, deals: list[dict] | None = None, company_deals: list[dict] | None = None) -> bool:
    """Paid/Signed at the person or company. Open pipeline is not a client."""
    extra = ev.extra or {}
    if extra.get("closed_won") or extra.get("company_closed_won") or extra.get("company_has_paid"):
        return True
    return person_or_company_closed_won(deals, company_deals, extra)


def call_supports_proposal_sent(ev: Engagement, facts: dict | None = None) -> bool:
    """Held, priced call with a proposal promised or sent."""
    if ev.source not in {"fireflies", "cube_acr"}:
        return False
    if is_silent_meeting(ev) or not is_meeting_held(ev):
        return False
    facts = facts or {}
    terms = facts.get("deal_terms") if isinstance(facts.get("deal_terms"), dict) else {}
    from crmbrain.intelligence import tcv_from_terms

    amount = (
        str(facts.get("amount_hint") or facts.get("deal_amount") or "").strip()
        or tcv_from_terms(terms)
    )
    if not amount:
        return False
    status = str(terms.get("status") or "").strip().lower()
    if status in {"quoted", "accepted"}:
        return True
    blob = " ".join(
        [
            ev.summary or "",
            ev.transcript or "",
            ev.raw_subject or "",
            str(terms.get("quote") or ""),
            str(facts.get("stage_hint") or ""),
        ]
    ).lower()
    return any(h in blob for h in ("proposal", "sow", "statement of work", "pricing", "quote"))


def requires_josh_meeting_to_open_deal(ev: Engagement) -> bool:
    """HeyReach / client-campaign prospects need a held or scheduled meeting with Josh."""
    extra = ev.extra or {}
    if ev.source in NEVER_OPEN_DEAL_SOURCES or extra.get("client_campaign") or extra.get("heyreach"):
        return not (is_meeting_held(ev) or is_meeting_scheduled(ev))
    return False


def is_salesglider_intro(ev: Engagement) -> bool:
    blob = _blob(ev)
    return any(h in blob for h in SALESGLIDER_INTRO_HINTS)


def personal_allowed_for_sales_intro(ev: Engagement) -> bool:
    """Jeremy Ciotola is personal except an explicit SalesGlider Intro meeting."""
    name = (ev.display_name() or ev.name or "").lower()
    if "jeremy" not in name and "ciotola" not in name:
        return False
    return is_salesglider_intro(ev) and ev.source in {"calendly", "fireflies", "gmail"}


def is_discovery_meeting(ev: Engagement) -> bool:
    return any(h in _blob(ev) for h in DISCOVERY_HINTS)


def cube_transcript_usable(ev: Engagement) -> bool:
    """Real Cube transcript, not HTML scrape or family chat."""
    text = (ev.transcript or ev.summary or "").strip()
    if len(text) < 80:
        return False
    if looks_like_html(text):
        return False
    extra = ev.extra or {}
    if extra.get("transcript_kind") == "html_txt":
        return False
    low = text.lower()
    if any(x in low for x in FAMILY_ONLY_HINTS) and not any(x in low for x in BUSINESS_HINTS):
        return False
    return True


CONFIDENT_NO_INTENTS = frozenset(
    {
        "day_job",
        "vendor",
        "mentor",
        "recruiter",
        "learning",
        "personal",
        "hire",
        "contractor",
    }
)
HELD_CALL_SOURCES = frozenset({"cube_acr", "fireflies"})
BOOKED_MEETING_SOURCES = frozenset({"calendly", "fireflies", "cube_acr", "allo", "gmail"})


def cube_has_sales_intent(
    ev: Engagement,
    decision=None,
    min_confidence: float = 0.75,
) -> bool:
    """Strict discovery hints or a confident Gemini yes. Confident 'no' always wins."""
    from crmbrain.intent import is_confident_non_sales, is_confident_sales

    if decision is None:
        decision = getattr(ev, "_intent_decision", None)
    if decision is None:
        extra = ev.extra or {}
        if extra.get("intent_no"):
            return False
        from crmbrain.intent import heuristic_intent

        decision = heuristic_intent(ev)
    if is_confident_non_sales(decision, min_confidence):
        return False
    if getattr(decision, "intent", "") in CONFIDENT_NO_INTENTS and decision.verdict == "no":
        return False
    blob = f"{_blob(ev)} {(ev.transcript or '')[:4000]}"
    if has_word_hint(blob, STRICT_DISCOVERY_HINTS):
        return True
    extra = ev.extra or {}
    if extra.get("intent_gemini_yes"):
        return True
    if getattr(decision, "via", "heuristic") == "gemini" and is_confident_sales(decision, min_confidence):
        return True
    return False


def contact_is_prospect(contact: dict | None, deals: list[dict] | None = None) -> bool:
    """Open pre-sale deal and no Signed/Paid. crm_source alone does not count."""
    if not contact:
        return False
    if has_closed_won_deal(deals):
        return False
    allowed = PRE_SALE_STAGES | {STAGE["proposal_sent"]}
    for deal in live_open_deals(deals or []):
        stage = (deal.get("properties") or {}).get("dealstage") or ""
        if stage in allowed:
            return True
    return False


def has_paperwork_evidence(ev: Engagement) -> bool:
    """A real proposal/contract/invoice document — not the words in a call."""
    extra = ev.extra or {}
    if extra.get("document_id") or extra.get("document_name"):
        return True
    return False


def is_new_completed_paperwork(ev: Engagement) -> bool:
    """Completed PandaDoc / DocuSign for a new engagement — the only create on a Paid client."""
    extra = ev.extra or {}
    if extra.get("payment") or extra.get("amount_source") == "payment":
        return False
    if ev.source != "gmail":
        return False
    stage = ev.stage_hint or extra.get("stage") or ""
    if stage not in {STAGE["signed"], "signed", "closedwon"}:
        return False
    return bool(extra.get("document_id") or extra.get("document_name") or extra.get("completed_doc"))


def is_payment_event(ev: Engagement) -> bool:
    extra = ev.extra or {}
    if extra.get("payment") or extra.get("amount_source") == "payment":
        return True
    return ev.stage_hint == STAGE["paid"]


def closed_won_notes_only(
    ev: Engagement,
    deals: list[dict] | None,
    contact: dict | None = None,
    company_deals: list[dict] | None = None,
) -> bool:
    """Paid/Signed at the person or company: notes only. Never a new Gmail/call deal.

    Payment may update an existing closed-won deal. New completed paperwork may
    open a new engagement. Everything else is notes / review.
    """
    del contact
    if is_new_completed_paperwork(ev) or is_payment_event(ev):
        return False
    return person_or_company_closed_won(deals, company_deals, ev.extra or {})


def is_cube_business_discovery(ev: Engagement, *, already_prospect: bool | None = None) -> bool:
    """Held Discovery only for sales intent, or a 1:1 with an existing prospect.

    Routine client/partner calls must not create deals.
    """
    if not cube_transcript_usable(ev):
        return False
    return held_call_may_open_deal(ev, already_prospect=already_prospect)


def held_call_may_open_deal(ev: Engagement, *, already_prospect: bool | None = None) -> bool:
    """Cube/Fireflies may open a deal only on sales intent or an existing prospect."""
    sales = cube_has_sales_intent(ev)
    if is_client_context_ev(ev) and not sales:
        return False
    if sales:
        return True
    if already_prospect is None:
        already_prospect = bool((ev.extra or {}).get("already_prospect"))
    return bool(already_prospect)


def only_held_call_evidence(engagements: list) -> bool:
    """True when the person's meeting evidence is Cube/Fireflies only."""
    sources = {getattr(ev, "source", "") for ev in engagements or []}
    meeting = {s for s in sources if s in BOOKED_MEETING_SOURCES}
    return bool(meeting) and meeting <= HELD_CALL_SOURCES


POC_HINTS = (
    "poc",
    "proof of concept",
    "pilot",
    "kickoff",
    "onboarding",
    "paid poc",
    "paid pilot",
)
POC_HINT_RE = re.compile(
    r"\b(?:poc|proof of concept|pilot|kickoff|onboarding|paid poc|paid pilot)\b",
    re.I,
)


def has_word_hint(blob: str, hints: tuple[str, ...] | list[str]) -> str:
    """Match hints on word boundaries so 'apocalypse' is not a POC."""
    text = blob or ""
    for hint in hints:
        if not hint:
            continue
        if re.search(rf"\b{re.escape(hint)}\b", text, re.I):
            return hint
    return ""


def has_poc_evidence(ev: Engagement) -> bool:
    blob = f"{_blob(ev)} {ev.transcript or ''}"
    return bool(POC_HINT_RE.search(blob))


def document_matches_deal(deal: dict | None, ev: Engagement) -> bool:
    """True only when THIS deal already stores the same document id or name."""
    if not deal:
        return False
    extra = ev.extra or {}
    incoming_id = str(extra.get("document_id") or "").strip().lower()
    incoming_name = str(extra.get("document_name") or "").strip().lower()
    props = deal.get("properties") or {}
    stored_id = str(props.get("document_id") or "").strip().lower()
    stored_name = str(props.get("document_name") or "").strip().lower()
    if incoming_id and stored_id and incoming_id == stored_id:
        return True
    if incoming_name and stored_name and incoming_name == stored_name:
        return True
    return False


def is_allo_discovery(ev: Engagement) -> bool:
    extra = ev.extra or {}
    duration = 0
    try:
        duration = int(extra.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0
    text = (ev.transcript or ev.summary or "").strip()
    if duration >= 45 and len(text) >= 40:
        return True
    return is_discovery_meeting(ev) and bool(text)


def is_meeting_held(ev: Engagement) -> bool:
    if is_call_screener(ev):
        return False
    if ev.source == "fireflies":
        return not is_silent_meeting(ev)
    if ev.source == "cube_acr":
        return is_cube_business_discovery(ev)
    if ev.source == "allo":
        return is_allo_discovery(ev)
    return False


def is_meeting_scheduled(ev: Engagement) -> bool:
    if ev.source == "calendly":
        return True
    if ev.stage_hint in {STAGE["discovery_scheduled"], "discovery_scheduled", "qualifiedtobuy"}:
        return True
    return False


def may_create_hubspot_contact(ev: Engagement) -> bool:
    if ev.source == "calendly":
        return True
    if ev.source == "fireflies":
        return True
    if ev.source == "cube_acr":
        return is_cube_business_discovery(ev)
    if ev.source == "allo":
        return is_allo_discovery(ev)
    return False


def may_write_hubspot(
    ev: Engagement, already_in_crm: bool, *, meeting_evidence: bool | None = None
) -> bool:
    """Write only for a meeting create, or an existing meeting-engaged contact.

    `already_in_crm` alone is not enough. A leftover Smartlead / HeyReach / RVM
    contact without held or scheduled meeting evidence must not be kept.
    When `meeting_evidence` is omitted, callers that already know the contact
    is meeting-engaged (notes backfill) keep the previous already-in-CRM path.
    """
    if may_create_hubspot_contact(ev):
        return True
    if not already_in_crm:
        return False
    if meeting_evidence is False:
        return False
    return True


def should_enroll_ticker_without_hubspot(ev: Engagement) -> bool:
    return ev.source in TICKER_WITHOUT_HUBSPOT


def is_explicit_back_signal(stage: str, ev: Engagement) -> bool:
    if stage not in BACK_STAGES:
        return False
    if is_meeting_held(ev) and stage == STAGE["no_show"]:
        return False
    if ev.source in NEVER_OPEN_DEAL_SOURCES and not (ev.stage_hint or ""):
        return False
    return True


def should_move_stage(current: str, target: str, *, back_signal: bool = False) -> bool:
    """Advance on stronger evidence. Never regress a held-meeting deal to Nurture or No Show."""
    if not target or current == target:
        return False
    held_floor = STAGE_RANK[STAGE["discovery_completed"]]
    if target in {STAGE["nurture"], STAGE["no_show"]} and STAGE_RANK.get(current, 0) >= held_floor:
        return False
    if target in BACK_STAGES and back_signal:
        return True
    if target in WEAK_STAGES and not back_signal:
        return False
    current_rank = STAGE_RANK.get(current, 0)
    target_rank = STAGE_RANK.get(target, 0)
    return target_rank > current_rank


MANUAL_SOURCE_TYPES = frozenset({"CRM_UI", "USER"})
INTEGRATION_SOURCE_TYPES = frozenset({"API", "INTEGRATION", "AUTOMATION_PLATFORM", "MERGE_OBJECTS"})


def _parse_hs_datetime(value: object) -> datetime | None:
    dt = parse_iso_datetime(value)
    if dt:
        return dt
    raw = str(value or "").strip()
    if not raw or not raw.isdigit():
        return None
    try:
        n = int(raw)
    except ValueError:
        return None
    if n > 10_000_000_000:
        n = n / 1000.0
    try:
        return datetime.fromtimestamp(n, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None


def last_manual_modification(deal: dict | None) -> datetime | None:
    """Last non-integration edit of stage or amount. Test hook: manual_modified_at."""
    if not deal:
        return None
    props = deal.get("properties") or {}
    hook = _parse_hs_datetime(props.get("manual_modified_at"))
    if hook:
        return hook
    latest: datetime | None = None
    history = deal.get("propertiesWithHistory") or {}
    for key in ("dealstage", "amount"):
        for row in history.get(key) or []:
            if not isinstance(row, dict):
                continue
            source = str(row.get("sourceType") or "").upper()
            user_id = row.get("updatedByUserId") or row.get("updatedByUser")
            if source in INTEGRATION_SOURCE_TYPES and not user_id:
                continue
            if source in MANUAL_SOURCE_TYPES or user_id:
                ts = _parse_hs_datetime(row.get("timestamp"))
                if ts and (latest is None or ts > latest):
                    latest = ts
    if latest:
        return latest
    if props.get("hs_updated_by_user_id"):
        return _parse_hs_datetime(props.get("hs_lastmodifieddate"))
    return None


def event_predates_manual_edit(ev: Engagement, deal: dict | None) -> bool:
    manual = last_manual_modification(deal)
    if not manual or not ev.occurred_at:
        return False
    occurred = _aware(ev.occurred_at)
    return bool(occurred and occurred < manual)


def deal_is_locked(deal: dict | None) -> bool:
    """HubSpot crmbrain_locked checkbox — never change stage, amount, or archive."""
    if not deal:
        return False
    raw = str((deal.get("properties") or {}).get("crmbrain_locked") or "").strip().lower()
    return raw in {"true", "1", "yes"}


def event_predates_freeze(ev: Engagement, settings: Settings | None) -> bool:
    freeze = getattr(settings, "manual_freeze_at", None) if settings else None
    if not freeze or not ev.occurred_at:
        return False
    occurred = _aware(ev.occurred_at)
    freeze_at = _aware(freeze)
    return bool(occurred and freeze_at and occurred < freeze_at)


def is_unidentified_cube_phone(ev: Engagement, contact: dict | None) -> bool:
    """Cube number with no HubSpot contact, no email, and no confident person name."""
    if ev.source != "cube_acr" or contact:
        return False
    if ev.email:
        return False
    if is_confident_person_name(ev.display_name() or ev.name):
        return False
    return bool(ev.phone)


def stamp_deal_context(
    ev: Engagement,
    contact: dict | None,
    deals: list[dict] | None,
    company_deals: list[dict] | None = None,
) -> None:
    """Mark HubSpot deal-holder flags before intent classification."""
    extra = dict(ev.extra or {})
    extra["closed_won"] = has_closed_won_deal(deals)
    extra["company_closed_won"] = has_closed_won_deal(company_deals)
    extra["company_has_paid"] = extra["company_closed_won"]
    extra["already_prospect"] = contact_is_prospect(contact, deals)
    extra["has_sg_deal"] = bool(
        live_open_deals(deals) or has_closed_won_deal(deals) or has_closed_won_deal(company_deals)
    )
    ev.extra = extra


def contact_has_any_deal(deals: list[dict] | None) -> bool:
    return bool(deals)


def may_mutate_existing_deal(
    ev: Engagement, deal: dict | None, settings: Settings | None = None
) -> bool:
    """False when a lock, freeze, or later manual edit blocks stage/amount writes."""
    if not deal:
        return True
    if deal_is_locked(deal):
        return False
    if event_predates_freeze(ev, settings):
        return False
    if event_predates_manual_edit(ev, deal):
        return False
    return True


def may_open_new_deal(
    ev: Engagement,
    contact: dict | None,
    deals: list[dict] | None,
    settings: Settings | None = None,
    company_deals: list[dict] | None = None,
) -> tuple[bool, str]:
    """Single create/restore gate used by cycle and reconcile (including dry-run)."""
    if is_unidentified_cube_phone(ev, contact):
        return False, "unknown_phone"
    if is_excluded_contact(ev, contact):
        return False, "not_deal"
    if row_has_not_deal_note(contact):
        return False, "not_deal"
    if closed_won_notes_only(ev, deals, contact=contact, company_deals=company_deals):
        return False, "closed_won"
    if any(deal_is_locked(d) for d in (deals or [])):
        return False, "locked"
    if event_predates_freeze(ev, settings) and contact_has_any_deal(deals):
        return False, "manual_freeze"
    if not contact and not (is_meeting_held(ev) or is_meeting_scheduled(ev)):
        return False, "no_contact_no_meeting"
    if (
        is_client_context_ev(ev)
        and is_closed_won_client(ev, deals, company_deals)
        and not is_new_completed_paperwork(ev)
        and not is_payment_event(ev)
    ):
        return False, "client"
    if ev.source in NEVER_OPEN_DEAL_SOURCES:
        return False, "cold_source"
    return True, ""


def choose_deal_action(
    current: str | None,
    requested: str,
    ev: Engagement,
    deal: dict | None = None,
    settings: Settings | None = None,
) -> str | None:
    """Stage to write, or None to leave the deal / skip create."""
    if not requested:
        return None
    if deal and not may_mutate_existing_deal(ev, deal, settings):
        return None
    if requires_josh_meeting_to_open_deal(ev) and not current:
        return None
    held = is_meeting_held(ev)
    target = requested
    if held and target in {STAGE["nurture"], STAGE["no_show"]}:
        if current not in {STAGE["nurture"], STAGE["closed_lost"]}:
            target = STAGE["discovery_completed"]
    if current == STAGE["replied"] and held:
        target = STAGE["discovery_completed"]
    if current in {STAGE["nurture"], STAGE["closed_lost"]}:
        call_forward = ev.source in {"fireflies", "cube_acr"} and target in {
            STAGE["discovery_completed"],
            STAGE["discovery_scheduled"],
            STAGE["proposal_sent"],
        }
        if target == STAGE["discovery_completed"] or call_forward:
            if target == STAGE["discovery_completed"] and not held:
                return None
            if call_forward and not held:
                return None
            # Manual edits win. No history → do not pull Nurture / Closed Lost.
            manual = last_manual_modification(deal)
            if not manual:
                return None
            if ev.occurred_at and _aware(ev.occurred_at) and _aware(ev.occurred_at) <= manual:
                return None
    if not current:
        if ev.source in NEVER_OPEN_DEAL_SOURCES:
            return None
        if target == STAGE["replied"]:
            return None
        return target
    if current == target:
        return None
    if current == STAGE["paid"] and target != STAGE["paid"]:
        return None
    if current == STAGE["signed"] and target not in {STAGE["signed"], STAGE["paid"]}:
        return None
    if current == STAGE["proposal_sent"] and target in {
        STAGE["discovery_completed"],
        STAGE["discovery_scheduled"],
    }:
        return None
    back = is_explicit_back_signal(requested, ev) or is_explicit_back_signal(target, ev)
    if not should_move_stage(current, target, back_signal=back):
        return None
    return target


def resolve_stage(ev: Engagement, facts: dict | None = None) -> str:
    """Only set a stage when evidence warrants it. No HeyReach/RVM Replied. No Smartlead Nurture."""
    facts = facts or {}
    if is_client_context(ev.display_name(), ev.company, ev.raw_subject) and is_closed_won_client(ev):
        return ""
    hint = facts.get("stage_hint") or ev.stage_hint
    stage = stage_id(hint) if hint else ""
    if ev.source == "fireflies" and is_silent_meeting(ev):
        return ""
    if ev.source != "gmail" and stage in MONEY_STAGES:
        if stage == STAGE["proposal_sent"] and call_supports_proposal_sent(ev, facts):
            pass
        else:
            stage = ""
    if stage in {STAGE["nurture"], STAGE["no_show"]} and is_meeting_held(ev):
        return STAGE["discovery_completed"]
    if stage:
        if ev.source in NEVER_OPEN_DEAL_SOURCES and stage in {STAGE["replied"], STAGE["nurture"]}:
            return ""
        return stage
    if ev.source == "calendly":
        return STAGE["discovery_scheduled"]
    if ev.source == "fireflies":
        if is_silent_meeting(ev):
            return ""
        if call_supports_proposal_sent(ev, facts):
            return STAGE["proposal_sent"]
        return STAGE["discovery_completed"]
    if ev.source == "cube_acr" and is_cube_business_discovery(ev):
        return STAGE["discovery_completed"]
    if ev.source == "allo" and is_allo_discovery(ev):
        return STAGE["discovery_completed"]
    return ""


def _norm_email(email: str | None) -> str:
    return (email or "").strip().lower()


def _domain_of(email: str = "", domain: str = "") -> str:
    raw = (domain or "").strip().lower()
    if raw:
        return raw.split("@")[-1]
    email_l = _norm_email(email)
    if "@" in email_l:
        return email_l.rsplit("@", 1)[-1]
    return ""


def _name_key(first: str = "", last: str = "", name: str = "") -> str:
    full = f"{(first or '').strip()} {(last or '').strip()}".strip() or (name or "").strip()
    return " ".join(full.lower().split())


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_iso_datetime(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return _aware(value)
    raw = str(value).strip().replace("Z", "+00:00")
    try:
        return _aware(datetime.fromisoformat(raw))
    except ValueError:
        return None


def scheduled_at_from_engagement(ev: Engagement) -> datetime | None:
    extra = ev.extra or {}
    return parse_iso_datetime(extra.get("meeting_at") or extra.get("scheduled_at"))


def held_near_scheduled(
    held_at: datetime | None,
    scheduled_at: datetime | None,
    *,
    window: timedelta = HELD_MATCH_WINDOW,
    require_scheduled: bool = False,
) -> bool:
    """True when a held call lines up with the booked slot.

    Email matches may omit scheduled_at. Name-only matches must pass
    ``require_scheduled=True`` so a missing scheduled time is not a match.
    """
    held_at = _aware(held_at)
    scheduled_at = _aware(scheduled_at)
    if require_scheduled and not scheduled_at:
        return False
    if not held_at or not scheduled_at:
        return not require_scheduled
    return abs(held_at - scheduled_at) <= window


def scheduled_past_grace(
    scheduled_at: datetime | None,
    now: datetime | None = None,
    *,
    grace: timedelta = NO_SHOW_GRACE,
) -> bool:
    scheduled_at = _aware(scheduled_at)
    if not scheduled_at:
        return True
    now = _aware(now) or now_utc()
    return now >= scheduled_at + grace


def _usable_match_domain(domain: str) -> bool:
    raw = (domain or "").strip().lower()
    if not raw:
        return False
    return raw not in FREE_MAIL_DOMAINS and raw not in JOSH_DOMAINS


def _is_josh_name(name: str) -> bool:
    key = _name_key(name=name)
    return bool(key) and (key in JOSH_NAME_KEYS or key.startswith("joshua osborn"))


def _excluded_held_email(email: str) -> bool:
    low = _norm_email(email)
    if not low:
        return False
    if low in JOSH_EMAILS or is_system_address_local(low) or is_notetaker_email_local(low):
        return True
    return _domain_of(low) in JOSH_DOMAINS


def is_system_address_local(email: str) -> bool:
    low = _norm_email(email)
    if not low or low in JOSH_EMAILS:
        return True
    if any(
        h in low
        for h in (
            "noreply",
            "no-reply",
            "donotreply",
            "mailer-daemon",
            "notifications@",
            "calendar-notification",
            "calendar-noreply",
            "@calendar.google.com",
        )
    ):
        return True
    domain = _domain_of(low)
    if domain in {"calendar.google.com", "googlemail.com"}:
        return True
    if is_zoom_room_address(low):
        return True
    local = low.split("@", 1)[0]
    if local.startswith("noreply") or local.startswith("no-reply") or local.startswith("donotreply"):
        return True
    return False


def is_notetaker_email_local(email: str) -> bool:
    low = _norm_email(email)
    if not low or "@" not in low:
        return False
    domain = _domain_of(low)
    if domain in _NOTETAKER_DOMAINS:
        return True
    return "notetaker" in low


def _drop_held_identity(email: str, name: str) -> bool:
    if email and _excluded_held_email(email):
        return True
    if _is_josh_name(name):
        return True
    if "notetaker" in (name or ""):
        return True
    if name and looks_like_meeting_title(name):
        return True
    return False


def _held_attendee_pairs(ev: Engagement) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    own_name = _name_key(ev.first_name, ev.last_name, ev.name or ev.display_name())
    own_email = _norm_email(ev.email)
    if own_name or own_email:
        pairs.append((own_name, own_email))
    extra = ev.extra or {}
    for raw in extra.get("participants") or []:
        display, email = parse_attendee_token(str(raw))
        pairs.append((_name_key(name=display), _norm_email(email)))
    for raw in extra.get("meeting_attendees") or []:
        if isinstance(raw, dict):
            display = (raw.get("displayName") or raw.get("name") or "").strip()
            email = _norm_email(raw.get("email") or "")
        else:
            display, email = parse_attendee_token(str(raw))
            email = _norm_email(email)
        pairs.append((_name_key(name=display), email))
    return pairs


def _kept_held_pairs(ev: Engagement) -> list[tuple[str, str]]:
    kept: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for name, email in _held_attendee_pairs(ev):
        if _drop_held_identity(email, name):
            continue
        key = (name, email)
        if key in seen:
            continue
        seen.add(key)
        kept.append((name, email))
    return kept


def held_participant_emails(ev: Engagement) -> set[str]:
    return {email for _name, email in _kept_held_pairs(ev) if email}


def held_participant_names(ev: Engagement) -> set[str]:
    return {name for name, _email in _kept_held_pairs(ev) if name}


def _prospect_emails(prospect: Engagement, contact: dict | None = None) -> set[str]:
    emails = {_norm_email(prospect.email)}
    if contact:
        emails.add(_norm_email(((contact.get("properties") or {}).get("email")) or ""))
    return {e for e in emails if e}


def _prospect_name(prospect: Engagement, contact: dict | None = None) -> str:
    name = _name_key(prospect.first_name, prospect.last_name, prospect.name or prospect.display_name())
    if name:
        return name
    if not contact:
        return ""
    props = contact.get("properties") or {}
    return _name_key(props.get("firstname") or "", props.get("lastname") or "")


def _prospect_domain(prospect: Engagement, contact: dict | None = None) -> str:
    domain = _domain_of(prospect.email, prospect.domain)
    if domain:
        return domain
    if not contact:
        return ""
    props = contact.get("properties") or {}
    return _domain_of(props.get("email") or "")


def _prospect_company(prospect: Engagement, contact: dict | None = None) -> str:
    company = (prospect.company or "").strip().lower()
    if company:
        return company
    if not contact:
        return ""
    return ((contact.get("properties") or {}).get("company") or "").strip().lower()


def _names_align(left: str, right: str) -> bool:
    """Exact normalized first+last only. No first-initial fallback."""
    return bool(left) and left == right


def prospect_matches_held(
    held: Engagement,
    prospect: Engagement,
    contact: dict | None = None,
    *,
    scheduled_at: datetime | None = None,
) -> bool:
    """Match a held Fireflies/Cube call to the booked prospect.

    Prefer attendee email. Name-only matches need an exact first+last on
    exactly one non-Josh participant and a known scheduled time within 24h.
    """
    if not is_meeting_held(held):
        return False
    prospect_emails = _prospect_emails(prospect, contact)
    held_emails = held_participant_emails(held)
    if prospect_emails & held_emails:
        return held_near_scheduled(held.occurred_at, scheduled_at)
    prospect_name = _prospect_name(prospect, contact)
    kept = _kept_held_pairs(held)
    held_names = [name for name, _email in kept if name]
    if not prospect_name or prospect_name not in held_names:
        return False
    held_domain = _domain_of(held.email, held.domain)
    if held_domain and _excluded_held_email(held.email):
        held_domain = ""
    prospect_domain = _prospect_domain(prospect, contact)
    if _usable_match_domain(held_domain) and _usable_match_domain(prospect_domain) and held_domain == prospect_domain:
        return held_near_scheduled(held.occurred_at, scheduled_at)
    held_company = _prospect_company(held)
    prospect_company = _prospect_company(prospect, contact)
    if held_company and prospect_company and held_company == prospect_company:
        return held_near_scheduled(held.occurred_at, scheduled_at)
    if held_emails and prospect_emails:
        return False
    if not is_confident_person_name(prospect_name):
        return False
    if held_names.count(prospect_name) != 1:
        return False
    return held_near_scheduled(held.occurred_at, scheduled_at, require_scheduled=True)


def matching_held_event(
    prospect: Engagement,
    contact: dict | None,
    held_events: list[Engagement] | None,
    scheduled_at: datetime | None = None,
) -> Engagement | None:
    for held in held_events or []:
        if prospect_matches_held(held, prospect, contact, scheduled_at=scheduled_at):
            return held
    return None


def has_closed_won_deal(deals: list[dict] | None) -> bool:
    for deal in deals or []:
        stage = (deal.get("properties") or {}).get("dealstage") or ""
        if stage in CLOSED_WON_STAGES:
            return True
    return False


def row_has_not_deal_note(row: dict | None) -> bool:
    if not row:
        return False
    props = row.get("properties") or {}
    blob = " ".join(str(v) for v in list(props.values()) + [row.get("not_deal_note")] if v is not None)
    return has_not_deal_note(blob)


def person_or_company_closed_won(
    deals: list[dict] | None,
    company_deals: list[dict] | None = None,
    extra: dict | None = None,
) -> bool:
    extra = extra or {}
    if has_closed_won_deal(deals) or has_closed_won_deal(company_deals):
        return True
    return bool(extra.get("closed_won") or extra.get("company_closed_won") or extra.get("company_has_paid"))


def blocks_no_show_create(deals: list[dict] | None, stage: str) -> bool:
    """Do not open a No Show deal when the contact already has Paid/Signed."""
    return stage == STAGE["no_show"] and has_closed_won_deal(deals)


def no_show_write_stage(
    *,
    prospect: Engagement,
    contact: dict | None = None,
    current_stage: str = "",
    held_events: list[Engagement] | None = None,
    scheduled_at: datetime | None = None,
    now: datetime | None = None,
    has_reschedule: bool = False,
    already_processed: bool = False,
    has_closed_won: bool = False,
) -> str:
    """Stage to write for a No Show signal. Empty means skip the write.

    Only a held event matched in this cycle may promote to Discovery Completed.
    A stale processed no_show event must not re-fire or re-promote.
    """
    held = matching_held_event(prospect, contact, held_events, scheduled_at)
    if held and not is_meeting_held(held):
        held = None
    completed_or_better = STAGE_RANK.get(current_stage, 0) >= STAGE_RANK[STAGE["discovery_completed"]]
    if already_processed:
        if held:
            return STAGE["discovery_completed"]
        return ""
    if held:
        return STAGE["discovery_completed"]
    if completed_or_better or has_closed_won or current_stage in CLOSED_WON_STAGES:
        return ""
    if has_reschedule:
        return ""
    if scheduled_at and not scheduled_past_grace(scheduled_at, now):
        return ""
    return STAGE["no_show"]


_DEAL_NAME_NOISE = r"(replied|appointment scheduled|discovery scheduled)"


def clean_deal_name(name: str, fallback: str = "") -> str:
    """Strip leftover Replied / Discovery Scheduled labels. Empty names use fallback."""
    cleaned = (name or "").strip()
    fallback = (fallback or "").strip()
    if not cleaned:
        return fallback
    cleaned = re.sub(rf"[\s]*[-–—][\s]*{_DEAL_NAME_NOISE}\s*$", "", cleaned, flags=re.I)
    cleaned = re.sub(rf"\s+\({_DEAL_NAME_NOISE}\)\s*$", "", cleaned, flags=re.I)
    cleaned = re.sub(rf"^{_DEAL_NAME_NOISE}$", "", cleaned, flags=re.I)
    cleaned = re.sub(rf"\s+{_DEAL_NAME_NOISE}\s*$", "", cleaned, flags=re.I)
    cleaned = re.sub(r"[\s]*[-–—][\s]*$", "", cleaned)
    cleaned = cleaned.strip()
    if looks_like_meeting_title(cleaned):
        return fallback or ""
    return cleaned or fallback or name.strip()


def deal_name_for(ev: Engagement, contact: dict | None = None) -> str:
    props = (contact or {}).get("properties") or {}
    first = ev.first_name or props.get("firstname") or ""
    last = ev.last_name or props.get("lastname") or ""
    company = ev.company or props.get("company") or ""
    fallback = ""
    built = f"{first} {last}".strip()
    if not built:
        raw = ev.display_name()
        if raw and not looks_like_meeting_title(raw):
            fallback = raw
    return format_deal_name(first, last, company, fallback=fallback or ev.company or ev.email or "")


def deal_has_amount(deal: dict) -> bool:
    raw = (deal.get("properties") or {}).get("amount")
    if raw in (None, ""):
        return False
    try:
        return float(raw) > 0
    except (TypeError, ValueError):
        return bool(str(raw).strip())


def deal_pipeline(deal: dict) -> str:
    raw = ((deal.get("properties") or {}).get("pipeline") or DEFAULT_PIPELINE).strip()
    return raw.lower() or DEFAULT_PIPELINE


def is_commerce_or_subscription_deal(deal: dict) -> bool:
    props = deal.get("properties") or {}
    dealtype = (props.get("dealtype") or "").strip().lower()
    if dealtype in COMMERCE_DEALTYPES or "subscription" in dealtype:
        return True
    if (props.get("hs_is_closed_won") or "").strip().lower() in {"true", "1", "yes"}:
        return True
    for key in COMMERCE_AMOUNT_PROPS:
        if props.get(key) not in (None, "", 0, "0", "0.0"):
            return True
    return False


def is_collapsible_deal(deal: dict) -> bool:
    """Workflow-stub dupes only: default pipeline, pre-sale, no amount, not commerce."""
    props = deal.get("properties") or {}
    stage = props.get("dealstage") or ""
    if deal_pipeline(deal) != DEFAULT_PIPELINE:
        return False
    if stage not in PRE_SALE_STAGES:
        return False
    if stage in CLOSED_WON_STAGES:
        return False
    if deal_has_amount(deal):
        return False
    if is_commerce_or_subscription_deal(deal):
        return False
    return True


def live_open_deals(deals: list[dict] | None) -> list[dict]:
    live = []
    for deal in deals or []:
        stage = (deal.get("properties") or {}).get("dealstage") or ""
        if stage not in {STAGE["closed_lost"], STAGE["paid"]}:
            live.append(deal)
    return live


def closed_won_deals(deals: list[dict] | None) -> list[dict]:
    won = []
    for deal in deals or []:
        stage = (deal.get("properties") or {}).get("dealstage") or ""
        if stage in CLOSED_WON_STAGES:
            won.append(deal)
    return won


def deal_richness(deal: dict) -> tuple:
    """Higher is better. Amounted / named deals beat empty 'First Last -' stubs."""
    props = deal.get("properties") or {}
    name = (props.get("dealname") or "").strip()
    return (
        1 if deal_has_amount(deal) else 0,
        0 if is_weak_deal_name(name) else 1,
        len(name),
        str(deal.get("id") or ""),
    )


def pick_richer_deal(left: dict, right: dict) -> dict:
    return left if deal_richness(left) >= deal_richness(right) else right


def duplicate_open_deal_pairs(deals: list[dict]) -> list[tuple[dict, dict]]:
    """Same-stage default-pipeline stubs. Leave the whole group if any deal is unsafe."""
    groups: dict[tuple[str, str], list[dict]] = {}
    for deal in deals or []:
        props = deal.get("properties") or {}
        stage = props.get("dealstage") or ""
        if not stage:
            continue
        groups.setdefault((deal_pipeline(deal), stage), []).append(deal)
    pairs: list[tuple[dict, dict]] = []
    for group in groups.values():
        if len(group) < 2:
            continue
        if not all(is_collapsible_deal(d) for d in group):
            continue
        keep = group[0]
        for other in group[1:]:
            keep = pick_richer_deal(keep, other)
        for other in group:
            if str(other.get("id")) != str(keep.get("id")):
                pairs.append((keep, other))
    return pairs


def promote_replied_stage(
    contact: dict,
    *,
    has_real_meetings: bool = False,
    has_email_associations: bool = False,
    has_calendar_meeting: bool = False,
) -> str:
    """Stage to promote a Replied deal to, or empty to archive.

    Email associations are ignored. Only crm_source meeting evidence, a real
    HubSpot meeting engagement, or an upcoming/recent calendar attendee event
    can promote.
    """
    del has_email_associations  # never a reason to promote
    source = ((contact.get("properties") or {}).get("crm_source") or "").lower()
    if source in {"fireflies", "cube_acr", "allo"}:
        return STAGE["discovery_completed"]
    if source == "calendly":
        return STAGE["discovery_scheduled"]
    if has_real_meetings or has_calendar_meeting:
        return STAGE["discovery_scheduled"]
    return ""


def contact_has_meeting_evidence(contact: dict, deals: list[dict] | None = None) -> bool:
    props = contact.get("properties") or {}
    source = (props.get("crm_source") or "").lower()
    if source in MEETING_CRM_SOURCES:
        return True
    for deal in deals or []:
        st = (deal.get("properties") or {}).get("dealstage") or ""
        if st in MEETING_STAGES:
            return True
    return False


def is_blank_contact(contact: dict) -> bool:
    props = contact.get("properties") or {}
    identity = (
        (props.get("email") or "").strip()
        or (props.get("phone") or "").strip()
        or (props.get("firstname") or "").strip()
        or (props.get("lastname") or "").strip()
        or (props.get("company") or "").strip()
    )
    return not identity
