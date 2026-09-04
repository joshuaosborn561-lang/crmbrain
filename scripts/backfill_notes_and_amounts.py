#!/usr/bin/env python3
"""Reprocess recent Fireflies + Cube transcripts onto existing HubSpot contacts.

Fills relational notes and deal amount when the transcript clearly states a price.
Dry-run by default. Never creates a HubSpot contact that is not already in CRM.

    python scripts/backfill_notes_and_amounts.py
    python scripts/backfill_notes_and_amounts.py --apply --days 14
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crmbrain.config import Settings, is_personal, now_cdt  # noqa: E402
from crmbrain.hubspot import HubSpot  # noqa: E402
from crmbrain.intelligence import extract, merge_contact_props  # noqa: E402
from crmbrain.models import Engagement  # noqa: E402
from crmbrain.policy import may_write_hubspot, personal_allowed_for_sales_intro  # noqa: E402
from crmbrain.sources import cube_acr, fireflies  # noqa: E402


def date_range(days: int) -> list[str]:
    today = now_cdt().date()
    return [(today - timedelta(days=i)).isoformat() for i in range(max(1, days))]


def collect_meetings(settings: Settings, days: int) -> tuple[list[Engagement], list[str]]:
    errors: list[str] = []
    wide = replace(settings, lookback_hours=max(24, days * 24))
    out: list[Engagement] = []
    try:
        out.extend(fireflies.scan(wide, limit=100))
    except Exception as exc:
        errors.append(f"fireflies: {exc}")
    try:
        out.extend(cube_acr.scan(settings, dates=date_range(days)))
    except Exception as exc:
        errors.append(f"cube_acr: {exc}")
    return out, errors


def plan_row(ev: Engagement, contact: dict, facts: dict) -> dict:
    merged = merge_contact_props(contact, facts)
    amount = facts.get("amount_hint") or facts.get("deal_amount") or ""
    return {
        "source": ev.source,
        "external_id": ev.external_id,
        "contact_id": contact.get("id") or "",
        "name": ev.display_name() or ev.email or ev.phone,
        "note_fields": merged,
        "amount": amount,
    }


def apply_row(hs: HubSpot, row: dict) -> dict:
    wrote_notes = False
    wrote_amount = False
    if row.get("note_fields"):
        hs.patch_contact(row["contact_id"], row["note_fields"])
        wrote_notes = True
    amount = row.get("amount") or ""
    if amount:
        for deal in hs.open_deals_for_contact(row["contact_id"]):
            if hs.fill_deal_amount(deal, amount):
                wrote_amount = True
    return {"wrote_notes": wrote_notes, "wrote_amount": wrote_amount}


def run(
    apply: bool = False,
    days: int = 14,
    settings: Settings | None = None,
    hs: HubSpot | None = None,
    engagements: list[Engagement] | None = None,
) -> dict:
    settings = settings or Settings.from_env()
    errors: list[str] = []
    if engagements is None:
        engagements, collect_errors = collect_meetings(settings, days)
        errors.extend(collect_errors)
    if hs is None:
        if not settings.hubspot_token:
            return {
                "would_update": 0,
                "updated": 0,
                "wrote": apply,
                "rows": [],
                "skipped": [],
                "errors": errors + ["HUBSPOT_ACCESS_TOKEN missing"],
                "report": "HUBSPOT_ACCESS_TOKEN missing",
            }
        hs = HubSpot(settings)

    rows: list[dict] = []
    skipped: list[str] = []
    for ev in engagements:
        if ev.source not in {"fireflies", "cube_acr"}:
            skipped.append(f"{ev.source}:{ev.external_id} not a meeting transcript")
            continue
        if is_personal(name=ev.display_name(), phone=ev.phone, email=ev.email):
            if not personal_allowed_for_sales_intro(ev):
                skipped.append(f"{ev.source}:{ev.display_name() or ev.phone} personal")
                continue
        contact = hs.find_contact(email=ev.email, phone=ev.phone, name=ev.display_name())
        if not contact:
            skipped.append(f"{ev.source}:{ev.display_name() or ev.email or ev.phone} not in CRM")
            continue
        if not may_write_hubspot(ev, True):
            skipped.append(f"{ev.source}:{ev.external_id} skip HubSpot")
            continue
        facts = extract(settings, ev)
        row = plan_row(ev, contact, facts)
        if not row["note_fields"] and not row["amount"]:
            skipped.append(f"{ev.source}:{row['name']} nothing to write")
            continue
        rows.append(row)
        if apply:
            try:
                result = apply_row(hs, row)
                row.update(result)
            except Exception as exc:
                errors.append(f"{ev.source}:{ev.external_id}: {exc}")

    updated = sum(1 for r in rows if apply and (r.get("wrote_notes") or r.get("wrote_amount")))
    report = format_report(rows, skipped, errors, apply=apply, days=days, updated=updated)
    return {
        "would_update": len(rows),
        "updated": updated,
        "wrote": apply,
        "rows": rows,
        "skipped": skipped,
        "errors": errors,
        "report": report,
    }


def format_report(
    rows: list[dict],
    skipped: list[str],
    errors: list[str],
    *,
    apply: bool,
    days: int,
    updated: int,
) -> str:
    lines = [
        "Notes + amount backfill",
        f"mode: {'apply' if apply else 'dry-run'}",
        f"days: {days}",
        f"would_update: {len(rows)}",
    ]
    if apply:
        lines.append(f"updated: {updated}")
    for row in rows[:40]:
        fields = ",".join(sorted(row.get("note_fields") or {})) or "notes:none"
        amount = row.get("amount") or "amount:none"
        lines.append(f"  - {row.get('name')} [{row.get('source')}] {fields} {amount}")
    if len(rows) > 40:
        lines.append(f"  ... {len(rows) - 40} more")
    lines.append(f"skipped: {len(skipped)}")
    if errors:
        lines.append("errors:")
        for err in errors:
            lines.append(f"  - {err}")
    if not apply:
        lines.append("Nothing written. Pass --apply to PATCH existing HubSpot contacts/deals.")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write to HubSpot. Default is dry-run.")
    parser.add_argument("--days", type=int, default=14, help="How far back to re-read transcripts.")
    args = parser.parse_args()
    result = run(apply=args.apply, days=args.days)
    print(result["report"])
    return 1 if result.get("errors") else 0


if __name__ == "__main__":
    raise SystemExit(main())
