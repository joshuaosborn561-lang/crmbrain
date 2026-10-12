"""Josh-approved nurture draft format: recap opener, lane proof, question, guarantee."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from crmbrain.memory import Memory
from crmbrain.models import CycleReport
from crmbrain.nurture import (
    GENERAL_PROOF,
    LANE_PROOF_BANK,
    MEETING_GUARANTEE,
    NURTURE_QUESTION,
    NurtureDraft,
    ROOFING_PROOF,
    TRADES_PROOF,
    compose_nurture_draft,
    fire_due_rows,
    stored_valid_nurture_draft,
    validate_draft,
)
from tests.test_nurture_rebuild import FakeGmail, FakeSlack, make_settings


def _held_row(**extra) -> dict:
    row = {
        "name": "Pat Reyes",
        "email": "pat@summitroofs.test",
        "company": "Summit Roofs",
        "industry": "roofing",
        "reason": "met",
        "meeting_source": "fireflies",
        "fireflies": True,
        "fireflies_id": "ff-pat-website",
        "meeting_at": "2026-09-10T18:00:00+00:00",
        "summary": "Pat said he wants to get the website done first before more outbound.",
        "last_touch_snippet": "",
    }
    row.update(extra)
    return row


def _email_row(**extra) -> dict:
    row = {
        "name": "Lee Ng",
        "email": "lee@bytewise.test",
        "company": "Bytewise",
        "industry": "msp",
        "last_touch_snippet": "We just lost our SDR, maybe now.",
    }
    row.update(extra)
    return row


def test_approved_format_order_from_held_recap():
    draft = compose_nurture_draft(_held_row())
    parts = [p for p in draft.body.split("\n\n") if p.strip()]
    assert draft.valid is True
    assert parts[0].startswith("Hey Pat")
    assert "website" in parts[0].lower()
    assert parts[1].startswith(LANE_PROOF_BANK["roofing"][:20])
    assert parts[2] == NURTURE_QUESTION
    assert parts[3] == MEETING_GUARANTEE
    assert parts[4] == "Josh Osborn"
    assert "Worth a look?" not in draft.body
    assert "AirPods" not in draft.body
    assert "$2M" not in draft.body


def test_opener_from_own_email_not_generic_months():
    draft = compose_nurture_draft(_email_row())
    opener = draft.body.split("\n", 1)[0].lower()
    assert "sdr" in opener
    assert "it's been a few months" not in opener
    assert LANE_PROOF_BANK["msp"].split(",")[0] in draft.body
    assert draft.valid is True


def test_lane_proof_bank_and_aliases():
    roof = compose_nurture_draft(_email_row(industry="roofing", company="Summit Roofs"))
    cyber = compose_nurture_draft(_email_row(industry="cyber"))
    it_lane = compose_nurture_draft(_email_row(industry="it"))
    staff = compose_nurture_draft(
        _email_row(industry="recruiting", last_touch_snippet="Need to fill two AE seats before Q4.")
    )
    assert LANE_PROOF_BANK["roofing"] in roof.body
    assert LANE_PROOF_BANK["msp"] in cyber.body
    assert LANE_PROOF_BANK["msp"] in it_lane.body
    assert LANE_PROOF_BANK["staffing"] in staff.body
    assert roof.valid and cyber.valid and it_lane.valid and staff.valid


def test_no_recap_and_no_lane_proof_are_flagged():
    no_recap = compose_nurture_draft(
        {
            "name": "George Stradiota",
            "company": "Kelly Roofing",
            "industry": "roofing",
            "reason": "met",
            "meeting_at": "2026-04-30T17:00:00+00:00",
        }
    )
    assert no_recap.valid is False
    assert no_recap.reject_reason == "no_recap"
    assert LANE_PROOF_BANK["roofing"] in no_recap.body

    no_lane = compose_nurture_draft(
        {
            "name": "Joel Stewart",
            "company": "The Chill Brothers",
            "industry": "hvac",
            "last_touch_snippet": "Check back in the fall about filling shoulder season.",
        }
    )
    assert no_lane.valid is False
    assert no_lane.reject_reason == "no_lane_proof"
    assert "$2M" not in no_lane.body
    assert TRADES_PROOF.split(",")[0] not in no_lane.body
    assert GENERAL_PROOF.split(",")[0] not in no_lane.body


def test_declined_or_moved_invite_is_not_a_held_recap():
    row = {
        "name": "Dan McGurl",
        "email": "dan@example.com",
        "industry": "staffing",
        "reason": "booked",
        "gmail_messages": [
            {
                "subject": "Declined: SalesGlider Followup @ Fri Sep 4, 2026 10:00am (EDT)",
                "body": "When: Friday Sep 4, 2026 10:00am (Eastern Daylight Time)",
                "declined": True,
            }
        ],
    }
    draft = compose_nurture_draft(row)
    assert draft.valid is False
    assert draft.reject_reason == "no_recap"
    assert "sep 4" not in draft.body.lower()


def test_validate_draft_rejects_generic_proof_and_banned_copy():
    row = {"name": "Lee Ng", "last_touch_snippet": "We just lost our SDR, maybe now."}
    base = (
        "Hey Lee, you mentioned we just lost our SDR, maybe now.\n\n"
        f"{LANE_PROOF_BANK['msp']}\n\n"
        f"{NURTURE_QUESTION}\n\n"
        f"{MEETING_GUARANTEE}\n\n"
        "Josh Osborn"
    )
    ok = validate_draft(NurtureDraft(subject="Bytewise follow up", body=base), row)
    assert ok.valid is True

    two_m = validate_draft(
        NurtureDraft(subject="Bytewise follow up", body=base.replace(LANE_PROOF_BANK["msp"], GENERAL_PROOF)),
        row,
    )
    assert two_m.valid is False
    assert two_m.reject_reason == "generic_proof"

    old_roof = validate_draft(
        NurtureDraft(subject="Bytewise follow up", body=base.replace(LANE_PROOF_BANK["msp"], ROOFING_PROOF)),
        row,
    )
    assert old_roof.valid is False
    assert old_roof.reject_reason == "generic_proof"

    dash = validate_draft(
        NurtureDraft(subject="Bytewise follow up", body=base.replace("maybe now.", "maybe now — today.")),
        row,
    )
    assert dash.valid is False
    assert dash.reject_reason == "dash"

    dots = validate_draft(
        NurtureDraft(subject="Bytewise follow up", body=base.replace("maybe now.", "maybe now...")),
        row,
    )
    assert dots.valid is False
    assert dots.reject_reason == "ellipsis"

    poc = validate_draft(
        NurtureDraft(subject="Bytewise follow up", body=base + "\nHappy to run a free POC."),
        row,
    )
    assert poc.valid is False
    assert poc.reject_reason == "free_poc"

    pods = validate_draft(
        NurtureDraft(subject="Bytewise follow up", body=base + "\nAirPods just for chatting."),
        row,
    )
    assert pods.valid is False
    assert pods.reject_reason == "airpods"


def test_posting_cycle_keeps_stored_valid_draft(tmp_path: Path):
    settings = make_settings(nurture_post_enabled=True)
    memory = Memory(settings, data_dir=tmp_path)
    stored_body = (
        "Hey Lee, you mentioned the SDR seat is still open.\n\n"
        f"{LANE_PROOF_BANK['msp']}\n\n"
        f"{NURTURE_QUESTION}\n\n"
        f"{MEETING_GUARANTEE}\n\n"
        "Josh Osborn"
    )
    row = {
        "id": "t-keep",
        "name": "Lee Ng",
        "email": "lee@bytewise.test",
        "company": "Bytewise",
        "industry": "msp",
        "reason": "kicked_can",
        "status": "active",
        "next_fire_at": "2020-01-01T00:00:00+00:00",
        "signal_at": "2026-06-15T12:00:00+00:00",
        "last_touch_snippet": "We just lost our SDR, maybe now.",
        "draft_subject": "Bytewise follow up",
        "draft_body": stored_body,
    }
    assert stored_valid_nurture_draft(row) is not None
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
    kept = memory.get_ticker("t-keep")
    assert kept["draft_body"] == stored_body
    assert "SDR seat is still open" in kept["draft_body"]
    assert "We just lost our SDR" not in kept["draft_body"]


def test_invalid_stored_draft_is_not_kept(tmp_path: Path):
    settings = make_settings(nurture_post_enabled=True)
    memory = Memory(settings, data_dir=tmp_path)
    memory._local["ticker"] = [
        {
            "id": "t-rewrite",
            "name": "Pat Reyes",
            "email": "pat@summitroofs.test",
            "company": "Summit Roofs",
            "industry": "roofing",
            "status": "active",
            "next_fire_at": "2020-01-01T00:00:00+00:00",
            "signal_at": "2026-06-03T17:20:00+00:00",
            "last_touch_snippet": "Yes, send me info. Spring is our slow season.",
            "draft_subject": "Summit Roofs follow up",
            "draft_body": f"Hey Pat, stale copy with {GENERAL_PROOF}\n\n{MEETING_GUARANTEE}\n\nJosh Osborn",
        }
    ]
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
    stored = memory.get_ticker("t-rewrite")
    assert GENERAL_PROOF not in stored["draft_body"]
    assert LANE_PROOF_BANK["roofing"] in stored["draft_body"]
    assert stored_valid_nurture_draft(
        {"draft_body": stored["draft_body"], "draft_subject": stored["draft_subject"], "name": "Pat Reyes"}
    )
