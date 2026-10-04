"""Single gate for HubSpot deal writes and dry-run previews.

Every create, stage move, amount write, archive, and prune-move — live or
preview — must go through `authorize_deal_write` / `authorize_deal_lifecycle`
before `propose_deal_write` / `commit_deal_write` / `commit_amount_write` /
`commit_deal_archive` / `commit_deal_move`. Callers must not construct
ProposedWrite or call HubSpot.upsert_deal / fill_deal_amount / archive_deal /
move_deal directly.
"""

from __future__ import annotations

import re

from crmbrain.config import (
    STAGE,
    is_archived_hs_row,
    is_deleted_stage,
    is_excluded_contact,
    NO_SHOW_HINT,
)
from crmbrain.intelligence import deal_amount_to_write
from crmbrain.models import CycleReport, Engagement, ProposedWrite
from crmbrain.policy import (
    INCREMENT_NO_SHOW,
    choose_deal_action,
    closed_won_notes_only,
    deal_is_locked,
    event_predates_freeze,
    may_mutate_existing_deal,
    may_open_new_deal,
    row_has_not_deal_note,
)

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from crmbrain.config import Settings
    from crmbrain.hubspot import HubSpot

logger = logging.getLogger(__name__)

_HELD_SOURCE_RE = re.compile(r"\b(fireflies|cube_acr)\b", re.I)
_HELD_DATE_RE = re.compile(r"\b20\d{2}[-/]\d{1,2}[-/]\d{1,2}\b")
_HELD_ID_RE = re.compile(r"\b(?:ff[-_][\w-]+|cube[-_][\w-]+|[A-Za-z0-9_-]{6,})\b")


def held_evidence_reason(ev: Engagement | None, prefix: str = "") -> str:
    """Reason text that cites the held-meeting source plus a meeting id or date."""
    bits = [str(prefix or "").strip()]
    if ev is not None:
        bits.append(str(ev.source or "").strip())
        bits.append(str(ev.external_id or "").strip())
        when = ev.occurred_at
        if when is not None:
            try:
                bits.append(when.date().isoformat())
            except AttributeError:
                bits.append(str(when)[:10])
    return " ".join(b for b in bits if b)


def discovery_held_reason_allowed(reason: str) -> bool:
    """Discovery Held moves need a held source (fireflies/cube_acr) plus id or date."""
    text = reason or ""
    if not _HELD_SOURCE_RE.search(text):
        return False
    if _HELD_DATE_RE.search(text):
        return True
    leftover = _HELD_SOURCE_RE.sub(" ", text)
    leftover = re.sub(
        r"\b(held|meeting|beats|noshow|reextract|current|move|create|update|gmail|intro)\b",
        " ",
        leftover,
        flags=re.I,
    )
    return bool(_HELD_ID_RE.search(leftover))


def authorize_deal_lifecycle(
    deal: dict | None,
    *,
    ev: Engagement | None = None,
    settings: Settings | None = None,
    action: str = "archive",
) -> tuple[bool, str]:
    """Refuse locked deals and pre-freeze events for archive or move."""
    del action
    if not deal:
        return False, "no_deal"
    if deal_is_locked(deal):
        return False, "locked"
    if ev is not None and event_predates_freeze(ev, settings):
        return False, "manual_freeze"
    return True, ""


def record_lifecycle_refusal(
    deal: dict | None,
    reason: str,
    action: str,
    report: CycleReport | None = None,
) -> str:
    props = (deal or {}).get("properties") or {}
    label = str(props.get("dealname") or (deal or {}).get("id") or "deal")
    line = f"{label} {action} refused {reason}"
    logger.info(line)
    if report is not None and line not in report.review_queue:
        report.review_queue.append(line)
    return line


def commit_deal_archive(
    hs: HubSpot,
    deal: dict | None,
    *,
    ev: Engagement | None = None,
    settings: Settings | None = None,
    report: CycleReport | None = None,
) -> bool:
    settings = settings if settings is not None else getattr(hs, "settings", None)
    ok, reason = authorize_deal_lifecycle(deal, ev=ev, settings=settings, action="archive")
    if not ok:
        record_lifecycle_refusal(deal, reason, "archive", report=report)
        return False
    deal_id = str((deal or {}).get("id") or "")
    if not deal_id:
        return False
    hs.archive_deal(deal_id)
    return True


def commit_deal_move(
    hs: HubSpot,
    deal: dict | None,
    stage: str,
    *,
    evidence: str = "",
    dealname: str = "",
    ev: Engagement | None = None,
    settings: Settings | None = None,
    report: CycleReport | None = None,
) -> bool:
    settings = settings if settings is not None else getattr(hs, "settings", None)
    ok, reason = authorize_deal_lifecycle(deal, ev=ev, settings=settings, action="move")
    if not ok:
        record_lifecycle_refusal(deal, reason, "move", report=report)
        return False
    deal_id = str((deal or {}).get("id") or "")
    if not deal_id or not stage:
        return False
    if is_deleted_stage(stage) or stage in {NO_SHOW_HINT, "no_show", INCREMENT_NO_SHOW}:
        logger.warning("refuse dealstage write %s", stage)
        return False
    hs.move_deal(deal_id, stage, evidence=evidence, dealname=dealname)
    return True


def authorize_deal_write(
    ev: Engagement,
    *,
    requested_stage: str = "",
    amount: str = "",
    contact: dict | None = None,
    deal: dict | None = None,
    deals: list[dict] | None = None,
    company_deals: list[dict] | None = None,
    settings: Settings | None = None,
) -> tuple[str, str, str]:
    """Return (stage, amount, reason). Empty stage and amount means do not write.

    reason is `create`, `move`, or `amount` when a write is allowed. `refresh` means
    the existing deal may be touched for name cleanup only. Any other reason is a block.
    """
    deals = deals if deals is not None else ([deal] if deal else [])
    if (
        is_deleted_stage(requested_stage)
        or requested_stage in {NO_SHOW_HINT, "no_show", INCREMENT_NO_SHOW}
    ):
        return "", "", "deleted_stage"
    if is_excluded_contact(ev, contact) or row_has_not_deal_note(contact) or row_has_not_deal_note(deal):
        return "", "", "not_deal"
    if is_archived_hs_row(contact) or is_archived_hs_row(deal):
        return "", "", "archived"
    if closed_won_notes_only(ev, deals, contact=contact, company_deals=company_deals):
        return "", "", "closed_won"
    creating = deal is None
    if creating:
        ok, reason = may_open_new_deal(ev, contact, deals, settings, company_deals)
        if not ok:
            return "", "", reason
        stage = choose_deal_action(None, requested_stage, ev, settings=settings) if requested_stage else None
        if not stage:
            return "", "", "create_blocked"
        write_amount = ""
        if amount:
            write_amount = deal_amount_to_write(deal, str(amount), ev=ev) or ""
        return stage, write_amount, "create"
    if not may_mutate_existing_deal(ev, deal, settings):
        return "", "", "frozen"
    current = str((deal.get("properties") or {}).get("dealstage") or "")
    stage = ""
    if requested_stage:
        decided = choose_deal_action(current or None, requested_stage, ev, deal=deal, settings=settings)
        stage = decided or ""
    write_amount = ""
    if amount:
        write_amount = deal_amount_to_write(deal, str(amount), ev=ev) or ""
        if write_amount and not may_mutate_existing_deal(ev, deal, settings):
            write_amount = ""
    if not stage and not write_amount:
        return "", "", "refresh"
    return stage, write_amount, ("move" if stage else "amount")


def propose_deal_write(
    report: CycleReport,
    *,
    action: str,
    label: str,
    stage: str = "",
    amount: str = "",
    contact_id: str = "",
    deal_id: str = "",
    reason: str = "",
    ev: Engagement | None = None,
) -> None:
    if action in {"create", "restore"} and not stage:
        return
    if stage == STAGE["discovery_held"]:
        if ev is not None and ev.source in {"fireflies", "cube_acr"}:
            reason = held_evidence_reason(ev, prefix=reason)
        if not discovery_held_reason_allowed(reason):
            return
    if deal_id and stage:
        for existing in report.proposed_writes:
            if str(existing.get("deal_id") or "") == str(deal_id) and existing.get("stage") == stage:
                return
    report.proposed_writes.append(
        ProposedWrite(
            action=action,
            label=label,
            stage=stage,
            amount=amount,
            contact_id=contact_id,
            deal_id=deal_id,
            reason=reason,
        ).as_dict()
    )


def commit_deal_write(hs: HubSpot, contact: dict, ev: Engagement, stage: str, amount: str = "") -> dict:
    return hs.upsert_deal(contact, ev, stage, amount=amount)


def commit_amount_write(
    hs: HubSpot,
    deal: dict,
    amount: str,
    ev: Engagement | None = None,
    contact: dict | None = None,
) -> bool:
    return hs.fill_deal_amount(deal, amount, ev=ev, contact=contact)
