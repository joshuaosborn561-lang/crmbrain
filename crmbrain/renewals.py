"""Client Renewals pipeline. Separate from new-business Sales Pipeline deals.

Auto-create 30 days before contract_end_date. Call Scheduled from calendar /
Fireflies / Gmail invite evidence titled renewal/review. At Risk when
positive_replies_30d < 12. crmbrain never opens a new-business deal for an
existing Closed Won client.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from crmbrain.config import RENEWAL_PIPELINE, RENEWAL_STAGE, Settings, now_utc
from crmbrain.models import CycleReport, Engagement
from crmbrain.policy import CLOSED_WON_STAGES, canonicalize_stage, deal_is_locked, deal_pipeline

logger = logging.getLogger(__name__)

AT_RISK_REPLY_FLOOR = 12
RENEWAL_CREATE_LEAD_DAYS = 30
CALL_ALERT_DAYS = 14
RENEWAL_TITLE_HINTS = (
    "renewal",
    "renew ",
    "qbr",
    "quarterly business",
    "client review",
    "account review",
)
OPEN_RENEWAL_STAGES = {
    RENEWAL_STAGE["renewal_upcoming"],
    RENEWAL_STAGE["call_scheduled"],
    RENEWAL_STAGE["at_risk"],
}


def is_renewal_deal(deal: dict | None) -> bool:
    if not deal:
        return False
    return deal_pipeline(deal) == RENEWAL_PIPELINE


def open_renewal_deals(deals: list[dict] | None) -> list[dict]:
    out = []
    for deal in deals or []:
        if not is_renewal_deal(deal):
            continue
        stage = (deal.get("properties") or {}).get("dealstage") or ""
        if stage in OPEN_RENEWAL_STAGES:
            out.append(deal)
    return out


def has_existing_client_deal(deals: list[dict] | None, company_deals: list[dict] | None = None) -> bool:
    """Closed Won new-business or any Client Renewals deal — do not open another Sales deal."""
    for deal in list(deals or []) + list(company_deals or []):
        stage = canonicalize_stage((deal.get("properties") or {}).get("dealstage") or "")
        if stage in CLOSED_WON_STAGES:
            return True
        if is_renewal_deal(deal):
            return True
    return False


def is_renewal_meeting(ev: Engagement) -> bool:
    blob = f"{ev.raw_subject or ''} {ev.summary or ''} {ev.name or ''} {(ev.extra or {}).get('event_type') or ''}".lower()
    return any(h in blob for h in RENEWAL_TITLE_HINTS)


def _parse_date(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        try:
            n = int(raw)
            if n > 10_000_000_000:
                n = n / 1000.0
            return datetime.fromtimestamp(n, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def due_for_renewal_create(deal: dict, now: datetime | None = None) -> bool:
    """Closed Won with contract_end_date inside the 30-day window."""
    now = now or now_utc()
    props = deal.get("properties") or {}
    stage = canonicalize_stage(props.get("dealstage") or "")
    if stage not in CLOSED_WON_STAGES:
        return False
    end = _parse_date(props.get("contract_end_date"))
    if not end:
        return False
    delta = end - now
    return timedelta(0) <= delta <= timedelta(days=RENEWAL_CREATE_LEAD_DAYS)


def at_risk_needed(positive_replies_30d: object) -> bool:
    try:
        return int(float(str(positive_replies_30d).strip() or "0")) < AT_RISK_REPLY_FLOOR
    except (TypeError, ValueError):
        return False


def apply_positive_replies(hs, deal: dict, count: int) -> str:
    """Write positive_replies_30d. Move Upcoming/Call Scheduled → At Risk below 12."""
    if not deal or not deal.get("id") or deal_is_locked(deal):
        return ""
    hs.patch_deal(str(deal["id"]), {"positive_replies_30d": str(int(count))})
    deal.setdefault("properties", {})["positive_replies_30d"] = str(int(count))
    stage = (deal.get("properties") or {}).get("dealstage") or ""
    if (
        is_renewal_deal(deal)
        and stage in {RENEWAL_STAGE["renewal_upcoming"], RENEWAL_STAGE["call_scheduled"]}
        and at_risk_needed(count)
    ):
        hs.move_deal(str(deal["id"]), RENEWAL_STAGE["at_risk"], evidence="renewals:at_risk")
        deal["properties"]["dealstage"] = RENEWAL_STAGE["at_risk"]
        return RENEWAL_STAGE["at_risk"]
    return ""


def move_to_call_scheduled(hs, deal: dict, ev: Engagement | None = None) -> bool:
    if not deal or not deal.get("id") or deal_is_locked(deal):
        return False
    if not is_renewal_deal(deal):
        return False
    stage = (deal.get("properties") or {}).get("dealstage") or ""
    if stage not in {RENEWAL_STAGE["renewal_upcoming"], RENEWAL_STAGE["at_risk"]}:
        return False
    evidence = f"{ev.source}:{ev.external_id}" if ev else "renewals:call_scheduled"
    hs.move_deal(str(deal["id"]), RENEWAL_STAGE["call_scheduled"], evidence=evidence)
    deal.setdefault("properties", {})["dealstage"] = RENEWAL_STAGE["call_scheduled"]
    return True


def maybe_schedule_renewal_call(hs, deals: list[dict] | None, ev: Engagement) -> dict | None:
    if not is_renewal_meeting(ev):
        return None
    if ev.source not in {"calendly", "gmail", "fireflies", "cube_acr"}:
        return None
    for deal in open_renewal_deals(deals):
        if move_to_call_scheduled(hs, deal, ev):
            return deal
    return None


def _contact_id_of(deal: dict) -> str:
    ids = deal.get("contact_ids") or []
    if ids:
        return str(ids[0])
    return str(deal.get("contact_id") or "")


def create_renewal_deal(hs, contact: dict, source_deal: dict) -> dict:
    """One open renewal per client. Copies monthly_fee. Does not touch the won deal."""
    if not contact or not contact.get("id"):
        return {}
    if deal_is_locked(source_deal):
        return {}
    existing = open_renewal_deals(hs.open_deals_for_contact(contact["id"]))
    if existing:
        return existing[0]
    props = source_deal.get("properties") or {}
    monthly = props.get("monthly_fee") or ""
    name = (props.get("dealname") or "Client").split(" - ")[0].strip() or "Client"
    payload = {
        "dealname": f"{name} - Renewal",
        "dealstage": RENEWAL_STAGE["renewal_upcoming"],
        "pipeline": RENEWAL_PIPELINE,
        "sg_deal_type": "renewal",
    }
    if monthly:
        payload["monthly_fee"] = monthly
        payload["amount"] = monthly
    created = hs.create_pipeline_deal(str(contact["id"]), payload)
    return created or {}


def sweep(hs, report: CycleReport, settings: Settings | None = None, engagements: list[Engagement] | None = None) -> None:
    """Create due renewals, flag At Risk from stored reply counts, schedule calls."""
    del settings
    try:
        deals = list(
            hs.iter_deals(
                [
                    "dealname",
                    "dealstage",
                    "pipeline",
                    "monthly_fee",
                    "contract_end_date",
                    "positive_replies_30d",
                    "crmbrain_locked",
                ]
            )
        )
    except Exception as exc:
        report.errors.append(f"renewals: {exc}")
        return
    by_contact: dict[str, list[dict]] = {}
    for deal in deals:
        cid = _contact_id_of(deal)
        if cid:
            by_contact.setdefault(cid, []).append(deal)
        if is_renewal_deal(deal) and at_risk_needed((deal.get("properties") or {}).get("positive_replies_30d")):
            stage = (deal.get("properties") or {}).get("dealstage") or ""
            if stage in {RENEWAL_STAGE["renewal_upcoming"], RENEWAL_STAGE["call_scheduled"]}:
                if apply_positive_replies(
                    hs, deal, int(float((deal.get("properties") or {}).get("positive_replies_30d") or 0))
                ):
                    report.deals_moved.append(f"{(deal.get('properties') or {}).get('dealname')} -> at_risk")
    for deal in deals:
        if not due_for_renewal_create(deal):
            continue
        contacts = []
        if hasattr(hs, "contacts_for_deal"):
            try:
                contacts = hs.contacts_for_deal(str(deal.get("id") or "")) or []
            except Exception:
                contacts = []
        contact = contacts[0] if contacts else None
        if not contact and _contact_id_of(deal):
            contact = {"id": _contact_id_of(deal), "properties": {}}
        if not contact:
            continue
        cid = str(contact["id"])
        if open_renewal_deals(by_contact.get(cid) or []):
            continue
        created = create_renewal_deal(hs, contact, deal)
        if created.get("id"):
            report.deals_created = getattr(report, "deals_created", None) or []
            if isinstance(report.deals_created, list):
                report.deals_created.append(f"{(created.get('properties') or {}).get('dealname')} renewal")
            report.deals_moved.append(f"{(created.get('properties') or {}).get('dealname')} renewal created")
            by_contact.setdefault(cid, []).append(created)
    for ev in engagements or []:
        if not is_renewal_meeting(ev):
            continue
        # Contact-level move happens in cycle when deals are already loaded.
        del ev
