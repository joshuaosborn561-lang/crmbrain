"""Josh's HubSpot keep rules: meeting-held/scheduled only, honest stages."""

from __future__ import annotations

from crmbrain.config import STAGE, is_client_context
from crmbrain.intelligence import stage_id
from crmbrain.models import Engagement

# Sources that may CREATE a HubSpot contact (meeting booked or held).
HUBSPOT_CREATE_SOURCES = frozenset({"fireflies", "calendly", "cube_acr", "allo"})
# These never open a deal and never create a contact. Ticker is fine.
NEVER_OPEN_DEAL_SOURCES = frozenset({"smartlead", "heyreach", "rvm", "gmail_person"})
TICKER_WITHOUT_HUBSPOT = frozenset({"smartlead", "heyreach", "rvm"})
MEETING_CRM_SOURCES = frozenset({"fireflies", "calendly", "cube_acr", "allo"})

DISCOVERY_HINTS = (
    "salesglider intro",
    "sg intro",
    "discovery",
    "disco call",
    "disco ",
    "intro call",
    "salesglider",
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


def is_cube_business_discovery(ev: Engagement) -> bool:
    """Real Cube ACR disco: transcript.docx text, not HTML scrape, not family chat."""
    text = (ev.transcript or ev.summary or "").strip()
    if len(text) < 80:
        return False
    if looks_like_html(text):
        return False
    extra = ev.extra or {}
    if extra.get("transcript_kind") == "html_txt":
        return False
    low = text.lower()
    if is_discovery_meeting(ev):
        return True
    if any(x in low for x in FAMILY_ONLY_HINTS) and not any(x in low for x in BUSINESS_HINTS):
        return False
    return True


def is_allo_discovery(ev: Engagement) -> bool:
    return is_discovery_meeting(ev) and bool((ev.transcript or ev.summary or ev.raw_subject).strip())


def is_meeting_held(ev: Engagement) -> bool:
    if ev.source == "fireflies":
        return True
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


def may_write_hubspot(ev: Engagement, already_in_crm: bool) -> bool:
    return already_in_crm or may_create_hubspot_contact(ev)


def should_enroll_ticker_without_hubspot(ev: Engagement) -> bool:
    return ev.source in TICKER_WITHOUT_HUBSPOT


def is_explicit_back_signal(stage: str, ev: Engagement) -> bool:
    if stage not in BACK_STAGES:
        return False
    if ev.source in NEVER_OPEN_DEAL_SOURCES and not (ev.stage_hint or ""):
        return False
    return True


def should_move_stage(current: str, target: str, *, back_signal: bool = False) -> bool:
    """Advance on stronger evidence. Never regress to Replied/Nurture without a back-signal."""
    if not target or current == target:
        return False
    if target in BACK_STAGES and back_signal:
        return True
    if target in WEAK_STAGES and not back_signal:
        return False
    current_rank = STAGE_RANK.get(current, 0)
    target_rank = STAGE_RANK.get(target, 0)
    return target_rank > current_rank


def choose_deal_action(current: str | None, requested: str, ev: Engagement) -> str | None:
    """Stage to write, or None to leave the deal / skip create."""
    if not requested:
        return None
    held = is_meeting_held(ev)
    target = requested
    if current == STAGE["replied"] and held:
        target = STAGE["discovery_completed"]
    if not current:
        if ev.source in NEVER_OPEN_DEAL_SOURCES:
            return None
        if target == STAGE["replied"]:
            return None
        return target
    if current == target:
        return None
    back = is_explicit_back_signal(requested, ev) or is_explicit_back_signal(target, ev)
    if not should_move_stage(current, target, back_signal=back):
        return None
    return target


def resolve_stage(ev: Engagement, facts: dict | None = None) -> str:
    """Only set a stage when evidence warrants it. No HeyReach/RVM Replied. No Smartlead Nurture."""
    facts = facts or {}
    if is_client_context(ev.display_name(), ev.company, ev.raw_subject):
        return ""
    hint = facts.get("stage_hint") or ev.stage_hint
    stage = stage_id(hint) if hint else ""
    if ev.source != "gmail" and stage in MONEY_STAGES:
        stage = ""
    if stage:
        if ev.source in NEVER_OPEN_DEAL_SOURCES and stage in {STAGE["replied"], STAGE["nurture"]}:
            return ""
        return stage
    if ev.source == "calendly":
        return STAGE["discovery_scheduled"]
    if ev.source == "fireflies":
        return STAGE["discovery_completed"]
    if ev.source == "cube_acr" and is_cube_business_discovery(ev):
        return STAGE["discovery_completed"]
    if ev.source == "allo" and is_allo_discovery(ev):
        return STAGE["discovery_completed"]
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
