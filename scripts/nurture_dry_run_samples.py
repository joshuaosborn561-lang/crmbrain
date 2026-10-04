#!/usr/bin/env python3
"""Write 10 sample #nurture cards (Block Kit + subject/body) from fixture-backed rows."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crmbrain.nurture import render_sample_cards, sample_card_rows  # noqa: E402


def main() -> int:
    rows = sample_card_rows()
    cards = render_sample_cards(rows)
    out = Path(__file__).resolve().parents[1] / "artifacts" / "nurture_sample_cards.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_from": "fixtures/nurture_fixtures.json real_sample + synthetics",
        "note": "No live HubSpot/Gmail tokens in this environment. Cards match compose_nurture_draft + build_nurture_card.",
        "count": len(cards),
        "mix": {
            "industry": sum(1 for c in cards if c.get("industry") in {"roofing", "hvac", "construction", "staffing", "msp"}),
            "general": sum(1 for c in cards if not c.get("industry")),
            "hubspot": sum(1 for c in cards if c.get("source") == "hubspot"),
            "gmail": sum(1 for c in cards if c.get("source") == "gmail"),
        },
        "cards": cards,
    }
    out.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"wrote {len(cards)} cards to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
