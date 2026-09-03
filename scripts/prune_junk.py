#!/usr/bin/env python3
"""Archive Appointment Scheduled junk deals with no meeting evidence.

Invoked from the daily cycle. Safe to run by hand:

    python scripts/prune_junk.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crmbrain.config import Settings  # noqa: E402
from crmbrain.hubspot import HubSpot  # noqa: E402
from crmbrain.models import CycleReport  # noqa: E402
from crmbrain.prune import run  # noqa: E402


def main() -> int:
    settings = Settings.from_env()
    if not settings.hubspot_token:
        print("HUBSPOT_ACCESS_TOKEN missing")
        return 2
    report = CycleReport()
    run(HubSpot(settings), report)
    print(report.summary_text())
    return 0 if not report.errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
