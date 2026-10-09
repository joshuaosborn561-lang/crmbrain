#!/usr/bin/env python3
"""Print (or apply) the Oct 8/9 sent-nurture ticker backfill.

Default is dry-run: prints the SQL in sql/20261009_backfill_nurture_sent.sql.
Pass --apply only after migrations/20261009_ticker_gmail_message_id.sql is on
crmbrain.ticker. Never run from the scheduled cycle.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SQL_PATH = ROOT / "sql" / "20261009_backfill_nurture_sent.sql"

SENT = [
    {
        "id": "fe1dba0a-7afb-4243-81ae-ca24af4d1653",
        "name": "Mike Dolan",
        "last_sent_at": "2026-10-08T22:23:06+00:00",
        "next_fire_at": "2027-01-06T22:23:06+00:00",
        "nurture_thread_id": "1a11d9cefa92102f",
        "nurture_thread_subject": "Roof River City follow up",
        "draft_subject": "Roof River City follow up",
    },
    {
        "id": "f0e03b45-f62f-4ca1-9b19-d09e72d6bacf",
        "name": "Lionel Francis",
        "last_sent_at": "2026-10-08T22:23:13+00:00",
        "next_fire_at": "2027-01-06T22:23:13+00:00",
        "nurture_thread_id": "1a11d9d0b07a9124",
        "nurture_thread_subject": "Empire Roofing follow up",
        "draft_subject": "Empire Roofing follow up",
    },
    {
        "id": "10e590f2-d4fc-43b1-828e-a0add6f51795",
        "name": "Josh Pugmire",
        "last_sent_at": "2026-10-09T12:15:07+00:00",
        "next_fire_at": "2027-01-07T12:15:07+00:00",
        "nurture_thread_id": "1a12096acb2664e6",
        "nurture_thread_subject": "Following up",
        "draft_subject": "Following up",
    },
    {
        "id": "4619b45f-d379-4932-bf86-b1afe3681951",
        "name": "Jonathan Matthews",
        "last_sent_at": "2026-10-09T12:15:11+00:00",
        "next_fire_at": "2027-01-07T12:15:11+00:00",
        "nurture_thread_id": "1a12096bacd6dfef",
        "nurture_thread_subject": "Following up",
        "draft_subject": "Following up",
    },
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="PATCH the four ticker rows via Memory. Default prints the SQL file.",
    )
    args = parser.parse_args()
    if not args.apply:
        print(SQL_PATH.read_text())
        print("Re-run with --apply after the gmail_message_id migration is live.")
        return 0

    sys.path.insert(0, str(ROOT))
    from crmbrain.config import Settings
    from crmbrain.memory import Memory

    settings = Settings.from_env()
    memory = Memory(settings)
    for row in SENT:
        thread = row["nurture_thread_id"]
        memory.patch_ticker(
            row["id"],
            {
                "nurture_state": "sent",
                "nurture_action": "approve",
                "stop_reason": "emailed_recently",
                "last_sent_at": row["last_sent_at"],
                "next_fire_at": row["next_fire_at"],
                "nurture_thread_id": thread,
                "gmail_thread_id": thread,
                "gmail_message_id": thread,
                "nurture_thread_subject": row["nurture_thread_subject"],
                "original_subject": row["nurture_thread_subject"],
                "draft_subject": row["draft_subject"],
                "thread_kind": "new_thread",
            },
        )
        print(f"patched {row['name']} {row['id']}")
    for err in memory.drain_errors():
        print(f"error: {err}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
