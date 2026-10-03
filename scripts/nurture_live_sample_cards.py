#!/usr/bin/env python3
"""Compose 10 live nurture sample cards from Gmail-backed rows (no HubSpot token)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crmbrain.config import STAGE
from crmbrain.nurture import compose_nurture_draft, infer_nurture_reason

NURTURE = STAGE["nurture"]

ROWS = [
    {
        "name": "Jackie Darkazalli",
        "email": "jackie@kellyroofing.com",
        "company": "Kelly Roofing",
        "industry": "roofing",
        "deal_stage": NURTURE,
        "reason": "never_booked",
        "fireflies": True,
        "meeting_at": "2026-04-10T20:30:00+00:00",
        "last_touch_snippet": "This week is getting away from me already as I'll be out for the Retail Live event. Can we schedule something for next week?",
        "gmail_thread_id": "19d261a9b9d771a1",
        "original_subject": "Follow up from our call earlier",
    },
    {
        "name": "Joel Stewart",
        "email": "joel@thechillbrothers.com",
        "company": "The Chill Brothers",
        "industry": "hvac",
        "deal_stage": NURTURE,
        "reason": "never_booked",
        "booked": True,
        "meeting_at": "2026-03-20T18:11:00+00:00",
        "last_touch_snippet": "",
        "gmail_thread_id": "19f2388d120027e0",
        "original_subject": "24 new referrals",
    },
    {
        "name": "Mike Dolan",
        "email": "mike@roofrivercity.com",
        "company": "Roof River City",
        "industry": "roofing",
        "deal_stage": NURTURE,
        "reason": "never_booked",
        "fireflies": True,
        "meeting_at": "2026-04-20T17:00:00+00:00",
        "last_touch_snippet": "",
        "gmail_thread_id": "19f237bb812bd703",
        "original_subject": "24 new referrals",
    },
    {
        "name": "Lionel Francis",
        "email": "lionel@empireroofing.com",
        "company": "Empire Roofing",
        "industry": "roofing",
        "deal_stage": NURTURE,
        "reason": "never_booked",
        "fireflies": True,
        "meeting_at": "2026-04-20T15:00:00+00:00",
        "last_touch_snippet": "",
        "gmail_thread_id": "19f237eb6d8cec78",
        "original_subject": "24 new referrals",
    },
    {
        "name": "Bradley Lord",
        "email": "bradley@empireroofing.com",
        "company": "Empire Roofing",
        "industry": "roofing",
        "deal_stage": NURTURE,
        "reason": "never_booked",
        "hs_meeting": True,
        "meeting_at": "2026-04-20T15:00:00+00:00",
        "last_touch_snippet": "bradley Lord",
        "gmail_thread_id": "",
        "original_subject": "",
    },
    {
        "name": "Kevin Hagemoser",
        "email": "kevin@kevinhagemoser.com",
        "company": "Kevin Hagemoser",
        "deal_stage": NURTURE,
        "reason": "never_booked",
        "met": True,
        "meeting_at": "2026-09-10T18:00:00+00:00",
        "last_touch_snippet": "Let's knock out a website. I will pick ONE offer and spend our focus on that for 90 days.",
        "gmail_thread_id": "19e89b556333fc67",
        "original_subject": "great call, thank you",
    },
    {
        "name": "Scott Hagan",
        "email": "shagan@finishlinestaffing.com",
        "company": "Finish Line Staffing",
        "industry": "staffing",
        "deal_stage": NURTURE,
        "reason": "never_booked",
        "booked": True,
        "fireflies": True,
        "meeting_at": "2026-08-03T16:30:00+00:00",
        "last_touch_snippet": "scott Hagan",
        "gmail_thread_id": "",
        "original_subject": "",
    },
    {
        "name": "Liz Schuerman",
        "email": "eschuerman@mbsicorp.com",
        "company": "MBSI",
        "deal_stage": NURTURE,
        "reason": "never_booked",
        "fireflies": True,
        "meeting_at": "2026-06-04T15:00:00+00:00",
        "last_touch_snippet": "",
        "gmail_thread_id": "",
        "original_subject": "",
    },
    {
        "name": "Jeremy Ciotola",
        "email": "jeremy.ciotola@gmail.com",
        "company": "",
        "deal_stage": NURTURE,
        "reason": "never_booked",
        "fireflies": True,
        "meeting_at": "2026-07-24T18:00:00+00:00",
        "last_touch_snippet": "jeremy Ciotola",
        "gmail_thread_id": "",
        "original_subject": "",
    },
    {
        "name": "Noah Brown",
        "email": "noahbbrown951@gmail.com",
        "company": "",
        "deal_stage": NURTURE,
        "reason": "never_booked",
        "fireflies": True,
        "meeting_at": "2026-04-17T15:00:00+00:00",
        "last_touch_snippet": "noah Brown",
        "gmail_thread_id": "",
        "original_subject": "",
    },
]


def main() -> int:
    cards = []
    for row in ROWS:
        reason = infer_nurture_reason(
            reason=str(row.get("reason") or ""),
            deal_stage=str(row.get("deal_stage") or ""),
            extra=row,
            booked=bool(row.get("booked")),
            met=bool(row.get("met")),
        )
        draft = compose_nurture_draft({**row, "reason": reason})
        thread_id = row.get("gmail_thread_id") or ""
        cards.append(
            {
                "name": row["name"],
                "email": row["email"],
                "reason": reason,
                "subject": draft.subject,
                "thread_id": thread_id,
                "body": draft.body,
            }
        )
    out = Path(__file__).resolve().parents[1] / "artifacts" / "nurture_live_sample_cards.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"count": len(cards), "cards": cards}, indent=2) + "\n")
    print(json.dumps(cards, indent=2))
    print(f"\nwrote {len(cards)} cards to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
