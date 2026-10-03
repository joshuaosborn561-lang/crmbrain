"""Single gate for HubSpot deal writes and dry-run previews.

Every create, stage move, and amount write — live or preview — must go through
`authorize_deal_write` before `propose_deal_write` / `commit_deal_write` /
`commit_amount_write`. Callers must not construct ProposedWrite or call
HubSpot.upsert_deal / fill_deal_amount directly.
"""

from __future__ import annotations

from crmbrain.config import is_excluded_contact
from crmbrain.intelligence import deal_amount_to_write
from crmbrain.models import CycleReport, Engagement, ProposedWrite
from crmbrain.policy import (
    choose_deal_action,
    closed_won_notes_only,
    may_mutate_existing_deal,
    may_open_new_deal,
    row_has_not_deal_note,
)

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from crmbrain.config import Settings
    from crmbrain.hubspot import HubSpot


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
    if is_excluded_contact(ev, contact) or row_has_not_deal_note(contact) or row_has_not_deal_note(deal):
        return "", "", "not_deal"
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
        return stage, str(amount or ""), "create"
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
) -> None:
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
