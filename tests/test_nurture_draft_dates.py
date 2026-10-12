"""Nurture last-touch dates: declined cancels, held needs a recap, header matches body."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from crmbrain.config import CDT
from crmbrain.memory import Memory
from crmbrain.models import CycleReport
from crmbrain.nurture import (
    NurtureDraft,
    build_nurture_card,
    compose_nurture_draft,
    fire_due_rows,
    last_touch_line,
    lookup_gmail_meeting_messages,
    resolve_last_touch,
    validate_draft,
)
from crmbrain.sources.gmail_scan import invite_timezone, parse_ics_dtstart, parse_meeting_at
from tests.test_nurture_rebuild import FakeGmail, FakeSlack, make_settings

NY = ZoneInfo("America/New_York")


def _dan_row(**extra) -> dict:
    row = {
        "id": "t-dan",
        "name": "Dan McGurl",
        "email": "dan@example.com",
        "company": "McGurl",
        "reason": "met",
        "status": "active",
        "nurture_state": "queued",
        "next_fire_at": "2020-01-01T00:00:00+00:00",
        "signal_at": "2026-08-18T17:00:00+00:00",
        "meeting_source": "fireflies",
        "fireflies": True,
        "fireflies_id": "ff-dan-sep1",
        "recordings": [
            {
                "source": "fireflies",
                "at": "2026-08-18T17:00:00+00:00",
                "summary": "401k lead gen for financial advisors.",
                "silent_meeting": False,
            },
            {
                "source": "fireflies",
                "at": "2026-09-01T17:00:00+00:00",
                "summary": "Follow-up on the proposal with the advisor team.",
                "silent_meeting": False,
            },
        ],
        "gmail_messages": [
            {
                "id": "m-dan-sep4-accepted",
                "subject": "Accepted: SalesGlider Followup @ Fri Sep 4, 2026 10:00am - 10:30am (EDT)",
                "body": "When: Friday Sep 4, 2026 10:00am (Eastern Daylight Time)",
                "from": "calendar-notification@google.com",
                "accepted": True,
            },
            {
                "id": "m-dan-sep4-declined",
                "subject": "Declined: SalesGlider Followup @ Fri Sep 4, 2026 10:00am - 10:30am (EDT)",
                "body": "When: Friday Sep 4, 2026 10:00am (Eastern Daylight Time)",
                "from": "calendar-notification@google.com",
                "declined": True,
            },
        ],
    }
    row.update(extra)
    return row


def _justin_row(**extra) -> dict:
    row = {
        "id": "t-justin",
        "name": "Justin Bechtel",
        "email": "justin@example.com",
        "company": "Bechtel",
        "reason": "met",
        "status": "active",
        "meeting_source": "fireflies",
        "fireflies": True,
        "fireflies_id": "ff-justin-jul30",
        "fireflies_at": "2026-07-30T16:00:00+00:00",
        "held_at": "2026-07-30T16:00:00+00:00",
        "recordings": [
            {
                "source": "fireflies",
                "at": "2026-07-30T16:00:00+00:00",
                "summary": "Walked the outbound plan.",
                "silent_meeting": False,
            },
            {
                "source": "fireflies",
                "at": "2026-08-14T16:00:00+00:00",
                "silent_meeting": True,
                "summary_status": "silent",
                "has_sentences": False,
                "summary": "",
            },
        ],
        "gmail_messages": [
            {
                "id": "m-justin-aug4-accepted",
                "subject": "Accepted: SalesGlider Intro @ Tue Aug 4, 2026 10:00am - 10:30am (EDT)",
                "body": "When: Tuesday Aug 4, 2026 10:00am (Eastern Daylight Time)",
                "from": "calendar-notification@google.com",
                "accepted": True,
            },
            {
                "id": "m-justin-aug4-moved",
                "subject": "Updated invitation: SalesGlider Intro @ Tue Aug 4, 2026 10:00am - 10:30am (EDT)",
                "body": "This event has been moved.",
                "from": "calendar-notification@google.com",
                "moved": True,
            },
        ],
    }
    row.update(extra)
    return row


def test_dan_mcgurl_held_sep_1_not_declined_sep_4():
    draft = compose_nurture_draft(_dan_row())
    opener = draft.body.split("\n", 1)[0].lower()
    touch = resolve_last_touch(_dan_row())
    assert touch.kind == "call"
    assert touch.phrase == "Sep 1"
    assert "sep 1" in opener
    assert "following up on our sep 1 call" in opener
    assert "sep 4" not in opener
    assert "aug 18" not in opener
    assert draft.valid is True


def test_justin_bechtel_held_jul_30_skips_moved_invite_and_silent_recap():
    draft = compose_nurture_draft(_justin_row())
    opener = draft.body.split("\n", 1)[0].lower()
    touch = resolve_last_touch(_justin_row())
    assert touch.kind == "call"
    assert touch.phrase == "Jul 30"
    assert "jul 30" in opener
    assert "following up on our jul 30 call" in opener
    assert "aug 4" not in opener
    assert "aug 14" not in opener
    assert draft.valid is True


def test_header_date_equals_body_date():
    for row in (_dan_row(), _justin_row()):
        draft = compose_nurture_draft(row)
        header = last_touch_line(row)
        phrase = resolve_last_touch(row).phrase
        opener = draft.body.split("\n", 1)[0]
        card = build_nurture_card(row, draft)
        context = card["blocks"][1]["elements"][0]["text"]
        assert phrase
        assert phrase in header
        assert phrase in opener
        assert header in context
        assert header.startswith("Last: call ")


def test_gmail_lookup_fetches_declined_messages():
    class _DeclinedGmail:
        def __init__(self):
            self.queries: list[str] = []

        def search(self, query, max_results=15):
            self.queries.append(query)
            return [
                {"id": "m-acc", "subject": "Accepted: SalesGlider Followup @ Fri Sep 4, 2026 10:00am (EDT)"},
                {"id": "m-dec", "subject": "Declined: SalesGlider Followup @ Fri Sep 4, 2026 10:00am (EDT)"},
            ]

        def get(self, message_id):
            return {"id": message_id, "snippet": "", "payload": {"headers": []}}

        def headers_map(self, message):
            mid = message["id"]
            if mid == "m-dec":
                return {"subject": "Declined: SalesGlider Followup @ Fri Sep 4, 2026 10:00am (EDT)", "from": "calendar-notification@google.com"}
            return {"subject": "Accepted: SalesGlider Followup @ Fri Sep 4, 2026 10:00am (EDT)", "from": "calendar-notification@google.com"}

        def body_text(self, message):
            return "When: Friday Sep 4, 2026 10:00am (Eastern Daylight Time)"

    gmail = _DeclinedGmail()
    messages = lookup_gmail_meeting_messages(gmail, email="dan@example.com")
    query = gmail.queries[0]
    assert "Declined:" in query
    assert "-subject:Declined" not in query
    assert any("Declined:" in str(m.get("subject") or "") for m in messages)


def test_invite_timezone_reads_edt():
    edt = parse_meeting_at(
        "Accepted: SalesGlider Followup @ Fri Sep 4, 2026 12:30am - 1:00am (EDT)",
        "When: Friday Sep 4, 2026 12:30am (Eastern Daylight Time)",
    )
    assert edt is not None
    assert str(edt.tzinfo) == "America/New_York"
    assert edt.astimezone(CDT).day == 3
    assert str(invite_timezone("(EDT) Eastern Daylight Time")) == "America/New_York"
    ics = parse_ics_dtstart("DTSTART;TZID=America/New_York:20260904T003000")
    assert ics is not None
    assert ics.tzinfo == NY
    assert ics.astimezone(CDT).day == 3


def test_card_post_persists_resolved_date_for_g7_approve(tmp_path: Path):
    settings = make_settings(nurture_post_enabled=True)
    memory = Memory(settings, data_dir=tmp_path)
    row = _dan_row()
    memory._local["ticker"] = [row]
    report = CycleReport()
    cards = fire_due_rows(
        settings,
        memory,
        report,
        now=datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc),
        gmail=FakeGmail(),
        slack=FakeSlack(),
    )
    assert cards
    stored = memory.get_ticker("t-dan")
    assert stored
    assert stored.get("meeting_at")
    stamp = datetime.fromisoformat(str(stored["meeting_at"]))
    assert stamp.astimezone(CDT).month == 9
    assert stamp.astimezone(CDT).day == 1
    assert stored.get("met") is True
    draft_body = stored.get("draft_body") or cards[0]["blocks"][3]["text"]["text"]
    assert "Sep 1" in (stored.get("draft_body") or "")
    slim = {
        "name": "Dan McGurl",
        "email": "dan@example.com",
        "meeting_at": stored["meeting_at"],
        "met": stored.get("met"),
        "booked": stored.get("booked"),
    }
    checked = validate_draft(
        NurtureDraft(subject=stored.get("draft_subject") or "McGurl follow up", body=stored["draft_body"]),
        slim,
    )
    assert checked.valid is True
    assert checked.reject_reason == ""
    del draft_body
