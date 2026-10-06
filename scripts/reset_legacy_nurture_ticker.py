#!/usr/bin/env python3
"""Stop active ticker rows with source IS NULL that are not HubSpot Nurture deals.

Dry-run by default (prints counts). Pass --apply to write stop_reason=legacy_reset.
Never run from the scheduled cycle.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crmbrain.config import STAGE, Settings  # noqa: E402
from crmbrain.memory import Memory  # noqa: E402
from crmbrain.nurture import apply_legacy_nurture_reset  # noqa: E402


def _nurture_deal_ids(settings: Settings) -> set[str]:
    if not settings.hubspot_token:
        return set()
    from crmbrain.hubspot import HubSpot
    from crmbrain.nurture import _SAMPLE_DEAL_PROPS

    hs = HubSpot(settings)
    deals = hs.search_objects(
        "deals",
        [{"propertyName": "dealstage", "operator": "EQ", "value": STAGE["nurture"]}],
        _SAMPLE_DEAL_PROPS,
        max_results=400,
    )
    return {str(d.get("id") or "").strip() for d in deals or [] if d.get("id")}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Stop matching ticker rows. Default is dry-run (print counts only).",
    )
    args = parser.parse_args()
    settings = Settings.from_env()
    memory = Memory(settings)
    nurture_ids = _nurture_deal_ids(settings)
    result = apply_legacy_nurture_reset(
        memory,
        nurture_deal_ids=nurture_ids or None,
        write=args.apply,
    )
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"{mode}: {result['count']} active source-null rows not mapped to a Nurture deal")
    for email, ticker_id in zip(result["emails"], result["ids"]):
        print(f"  {ticker_id or '-'} {email}")
    if not args.apply:
        print("Re-run with --apply to stop them (stop_reason=legacy_reset).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
