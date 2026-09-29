"""Josh's HubSpot keep rules: meeting-held/scheduled only, honest stages."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from crmbrain.config import STAGE, is_client_context, now_utc
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
HELD_CRM_SOURCES = frozenset({"fireflies", "cube_acr", "allo"})
NO_SHOW_GRACE = timedelta(hours=2)
HELD_MATCH_WINDOW = timedelta(hours=24)

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


def choose_deal_action(current: str | None, requested: str, ev: Engagement) -> str | None:
    """Stage to write, or None to leave the deal / skip create."""
    if not requested:
        return None
    held = is_meeting_held(ev)
    target = requested
    if held and target in {STAGE["nurture"], STAGE["no_show"]}:
        target = STAGE["discovery_completed"]
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
    if stage in {STAGE["nurture"], STAGE["no_show"]} and is_meeting_held(ev):
        return STAGE["discovery_completed"]
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
) -> bool:
    """True when a held call lines up with the booked slot (or either stamp is missing)."""
    held_at = _aware(held_at)
    scheduled_at = _aware(scheduled_at)
    if not held_at or not scheduled_at:
        return True
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


def held_participant_emails(ev: Engagement) -> set[str]:
    emails: set[str] = set()
    if ev.email:
        emails.add(_norm_email(ev.email))
    extra = ev.extra or {}
    for raw in extra.get("participants") or []:
        _display, email = parse_attendee_token(str(raw))
        if email:
            emails.add(_norm_email(email))
    for raw in extra.get("meeting_attendees") or []:
        if isinstance(raw, dict):
            email = _norm_email(raw.get("email") or "")
        else:
            _display, email = parse_attendee_token(str(raw))
            email = _norm_email(email)
        if email:
            emails.add(email)
    return {e for e in emails if e}


def held_participant_names(ev: Engagement) -> set[str]:
    names: set[str] = set()
    own = _name_key(ev.first_name, ev.last_name, ev.name or ev.display_name())
    if own:
        names.add(own)
    extra = ev.extra or {}
    for raw in extra.get("participants") or []:
        display, _email = parse_attendee_token(str(raw))
        key = _name_key(name=display)
        if key and not looks_like_meeting_title(display):
            names.add(key)
    for raw in extra.get("meeting_attendees") or []:
        if isinstance(raw, dict):
            display = (raw.get("displayName") or raw.get("name") or "").strip()
        else:
            display, _email = parse_attendee_token(str(raw))
        key = _name_key(name=display)
        if key and not looks_like_meeting_title(display):
            names.add(key)
    return names


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
    if not left or not right:
        return False
    if left == right:
        return True
    left_parts = left.split()
    right_parts = right.split()
    if len(left_parts) < 2 or len(right_parts) < 2:
        return False
    if left_parts[-1] != right_parts[-1]:
        return False
    return left_parts[0][0] == right_parts[0][0]


def prospect_matches_held(held: Engagement, prospect: Engagement, contact: dict | None = None) -> bool:
    """Match a held Fireflies/Cube call to the booked prospect.

    Prefer attendee email. When email is missing on either side, accept a
    confident name match plus domain or company.
    """
    if not is_meeting_held(held):
        return False
    prospect_emails = _prospect_emails(prospect, contact)
    if prospect_emails & held_participant_emails(held):
        return True
    prospect_name = _prospect_name(prospect, contact)
    held_names = held_participant_names(held)
    name_hit = any(_names_align(prospect_name, held_name) for held_name in held_names)
    if not name_hit:
        return False
    held_domain = _domain_of(held.email, held.domain)
    prospect_domain = _prospect_domain(prospect, contact)
    if held_domain and prospect_domain and held_domain == prospect_domain:
        return True
    held_company = (held.company or "").strip().lower()
    prospect_company = _prospect_company(prospect, contact)
    if held_company and prospect_company and (
        held_company == prospect_company
        or held_company in prospect_company
        or prospect_company in held_company
    ):
        return True
    # Email missing on the transcript: a confident first+last still counts.
    missing_email = not held.email or not prospect_emails
    if missing_email and prospect_name and is_confident_person_name(prospect_name):
        return any(held_name == prospect_name for held_name in held_names)
    return False


def matching_held_event(
    prospect: Engagement,
    contact: dict | None,
    held_events: list[Engagement] | None,
    scheduled_at: datetime | None = None,
) -> Engagement | None:
    for held in held_events or []:
        if not prospect_matches_held(held, prospect, contact):
            continue
        if held_near_scheduled(held.occurred_at, scheduled_at):
            return held
    return None


def contact_has_held_source(contact: dict | None) -> bool:
    if not contact:
        return False
    source = ((contact.get("properties") or {}).get("crm_source") or "").lower()
    return source in HELD_CRM_SOURCES


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
) -> str:
    """Stage to write for a No Show signal. Empty means skip the write.

    Held Fireflies/Cube evidence always wins. A stale processed no_show event
    must not re-fire; it may only recover a deal up to Discovery Completed.
    """
    held = matching_held_event(prospect, contact, held_events, scheduled_at)
    held_source = contact_has_held_source(contact)
    completed_or_better = STAGE_RANK.get(current_stage, 0) >= STAGE_RANK[STAGE["discovery_completed"]]
    if already_processed:
        if held or held_source:
            return STAGE["discovery_completed"]
        return ""
    if held or held_source:
        return STAGE["discovery_completed"]
    if completed_or_better:
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
