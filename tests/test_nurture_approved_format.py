"""Josh-approved nurture draft format: recap opener, lane proof, question, guarantee."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from crmbrain.memory import Memory
from crmbrain.models import CycleReport
import re

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
    resolve_nurture_company,
    stored_valid_nurture_draft,
    ticker_card_still_open,
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


def _opener_block(body: str) -> str:
    parts = [p for p in (body or "").split("\n\n") if p.strip()]
    if len(parts) >= 2 and parts[0].lower().startswith("hey "):
        return f"{parts[0]}\n\n{parts[1]}"
    return parts[0] if parts else ""


def _assert_complete_sentences(text: str) -> None:
    blob = re.sub(r"\s+", " ", (text or "").strip())
    assert blob
    assert not blob.endswith((",", ";", " and", " the", " to", " of"))
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", blob) if s.strip() and s.strip()[-1:] in ".!?"]
    assert sentences, text
    for sent in sentences:
        assert sent[0].isupper() or sent.startswith("Hey")
        assert sent[-1] in ".!?"


def test_approved_format_order_from_held_recap():
    draft = compose_nurture_draft(_held_row())
    parts = [p for p in draft.body.split("\n\n") if p.strip()]
    assert draft.valid is True
    opener = _opener_block(draft.body)
    assert opener.lower().startswith("hey pat")
    assert "website" in opener.lower()
    assert LANE_PROOF_BANK["roofing"][:20] in draft.body
    assert MEETING_GUARANTEE in draft.body
    assert parts[-1] == "Josh Osborn"
    assert "Worth a look?" not in draft.body
    assert "AirPods" not in draft.body
    assert "$2M" not in draft.body
    assert "meeting we had booked" not in draft.body.lower()


def test_opener_from_own_email_not_generic_months():
    draft = compose_nurture_draft(_email_row())
    opener = _opener_block(draft.body).lower()
    assert "sdr" in opener
    assert "it's been a few months" not in opener
    assert LANE_PROOF_BANK["msp"] in draft.body
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


def test_posting_cycle_composes_opener_from_recap_not_stored_draft(tmp_path: Path):
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
    assert kept["draft_body"] != stored_body
    assert "SDR seat is still open" not in kept["draft_body"]
    assert "sdr" in kept["draft_body"].lower()


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


def _live_ticker_row(
    name: str,
    email: str,
    company,
    industry,
    last_touch_snippet,
    meeting_at,
) -> dict:
    return {
        "name": name,
        "email": email,
        "company": company,
        "industry": industry,
        "last_touch_snippet": last_touch_snippet,
        "meeting_at": meeting_at,
        "draft_body": None,
    }


def _robert_row() -> dict:
    return _live_ticker_row(
        "Robert Lawson",
        "rob@cyberguard360.com",
        None,
        "msp",
        (
            "Aug 28 call: cold calling with Philippines-based reps is the main lead source; "
            "cold email underperforming on deliverability and spam filters; budget decision "
            "with cofounder Steve targeted early October."
        ),
        "2026-08-28T18:30:00+00:00",
    )


def _laura_row() -> dict:
    return _live_ticker_row(
        "Laura Klein",
        "lklein@grnplano.com",
        "GRN Plano",
        None,
        (
            "Sep 3 call: client acquisition is the bottleneck; needs at least 1 qualified "
            "meeting a week to justify cost; prefers service-based offers due to healthcare "
            "rules; hiring-ready clients close within a week."
        ),
        "2026-09-03T18:00:00+00:00",
    )


def _brian_row() -> dict:
    return _live_ticker_row(
        "Brian Donigan",
        "bdonigan@wtrenovations.com",
        "WT Renovations",
        "construction",
        (
            "Sep 25 call: commercial roofing to property owners and managers within 2-3 hours "
            "of KC, not GCs; team closes about 35% of leads; wants buy-ready appointments; "
            "testing in-house outreach this fall, revisit in February."
        ),
        None,
    )


def _justin_row() -> dict:
    return _live_ticker_row(
        "Justin Bechtel",
        "justin@onlyptrecruiting.com",
        "Only PT Recruiting",
        "staffing",
        "Last real conversation (late Jul/Aug): cold outbound and using AI agents for Only PT Recruiting.",
        None,
    )


def _vincent_row() -> dict:
    return _live_ticker_row(
        "Vincent Bowden",
        "vbowden@talentunify.com",
        None,
        "staffing",
        (
            "Sep 2 call: wants to diversify beyond HCM, PEO and payroll clients; adding junior "
            "recruiters for one-off clients and new verticals; reviewing with silent partner."
        ),
        "2026-09-02T17:00:00+00:00",
    )


ROBERT_OPENER = (
    "Hey Robert,\n\n"
    "When we talked on Aug 28 you said cold calling with Philippines-based reps is the "
    "main lead source. You said cold email underperforming on deliverability and spam "
    "filters. You mentioned budget decision with cofounder Steve targeted early October."
)
LAURA_OPENER = (
    "Hey Laura,\n\n"
    "When we talked on Sep 3 you said client acquisition is the bottleneck. You need at "
    "least 1 qualified meeting a week to justify cost. You prefer service-based offers "
    "due to healthcare rules."
)
VINCENT_OPENER = (
    "Hey Vincent,\n\n"
    "When we talked on Sep 2 you want to diversify beyond HCM, PEO and payroll clients. "
    "You are adding junior recruiters for one-off clients and new verticals. You are "
    "reviewing with silent partner."
)
JUSTIN_OPENER = (
    "Hey Justin,\n\n"
    "You mentioned cold outbound and using AI agents for Only PT Recruiting."
)
BRIAN_OPENER = (
    "Hey Brian,\n\n"
    "When we talked on Sep 25 you mentioned commercial roofing to property owners and "
    "managers within 2-3 hours of KC, not GCs. Your team closes about 35% of leads. You "
    "want buy-ready appointments."
)


def test_robert_lawson_live_row_opener_and_neutral_subject():
    draft = compose_nurture_draft(_robert_row())
    assert draft.valid is True
    assert _opener_block(draft.body) == ROBERT_OPENER
    assert draft.subject == "Robert follow up"
    assert "Cyberguard360" not in draft.subject
    assert "interested" not in draft.body.lower()
    assert LANE_PROOF_BANK["msp"] in draft.body
    assert "Where did you and Steve land?" in draft.body
    assert MEETING_GUARANTEE in draft.body


def test_laura_klein_live_row_staffing_and_qualified():
    draft = compose_nurture_draft(_laura_row())
    assert draft.valid is True
    assert draft.reject_reason != "no_lane_proof"
    assert _opener_block(draft.body) == LAURA_OPENER
    assert "qualified" in _opener_block(draft.body)
    assert "interested" not in _opener_block(draft.body).lower()
    assert draft.subject == "GRN Plano follow up"
    assert LANE_PROOF_BANK["staffing"] in draft.body


def test_vincent_bowden_live_row_strips_date_prefix():
    draft = compose_nurture_draft(_vincent_row())
    opener = _opener_block(draft.body)
    assert draft.valid is True
    assert opener == VINCENT_OPENER
    assert "sep 2 call:" not in opener.lower()
    assert "you sep 2" not in opener.lower()
    assert draft.subject == "Vincent follow up"
    assert "Talentunify" not in draft.subject
    assert LANE_PROOF_BANK["staffing"] in draft.body


def test_justin_bechtel_live_row_uses_snippet():
    draft = compose_nurture_draft(_justin_row())
    opener = _opener_block(draft.body)
    assert draft.valid is True
    assert opener == JUSTIN_OPENER
    assert "wanted to circle back" not in draft.body.lower()
    assert "last real conversation" not in draft.body.lower()
    assert LANE_PROOF_BANK["staffing"] in draft.body


def test_brian_donigan_live_row_keeps_recap_words():
    draft = compose_nurture_draft(_brian_row())
    opener = _opener_block(draft.body)
    assert draft.valid is True
    assert opener == BRIAN_OPENER
    assert "2-3 hours" in opener
    assert LANE_PROOF_BANK["roofing"] in draft.body


def test_subjects_use_hubspot_caps_or_neutral():
    robert = compose_nurture_draft(_robert_row())
    vincent = compose_nurture_draft(_vincent_row())
    assert robert.subject == "Robert follow up"
    assert vincent.subject == "Vincent follow up"
    assert "Cyberguard360" not in robert.subject
    assert "Talentunify" not in vincent.subject
    hs_robert = compose_nurture_draft({**_robert_row(), "associated_company": "CyberGuard360"})
    hs_vincent = compose_nurture_draft({**_vincent_row(), "associated_company": "TalentUnify"})
    assert hs_robert.subject == "CyberGuard360 follow up"
    assert hs_vincent.subject == "TalentUnify follow up"
    assert resolve_nurture_company("CyberGuard360", "", "rob@cyberguard360.com") == "CyberGuard360"
    assert resolve_nurture_company("TalentUnify", "", "vbowden@talentunify.com") == "TalentUnify"


def test_robert_opener_comes_from_recap_not_stored_draft_body():
    stored = (
        "Hey Robert, this stored draft should never be copied into the opener.\n\n"
        f"{LANE_PROOF_BANK['msp']}\n\n"
        "Where did you and Steve land?\n\n"
        f"{MEETING_GUARANTEE}\n\n"
        "Josh Osborn"
    )
    row = {**_robert_row(), "draft_subject": "Cyberguard360 follow up", "draft_body": stored}
    draft = compose_nurture_draft(row)
    assert _opener_block(draft.body) == ROBERT_OPENER
    assert stored.split("\n", 1)[0] not in draft.body
    assert draft.subject == "Robert follow up"


def test_fire_due_rows_skips_open_slack_card(tmp_path: Path):
    settings = make_settings(nurture_post_enabled=True)
    memory = Memory(settings, data_dir=tmp_path)
    row = {
        **_laura_row(),
        "id": "t-open-card",
        "status": "active",
        "next_fire_at": "2020-01-01T00:00:00+00:00",
        "signal_at": "2026-09-03T18:00:00+00:00",
        "slack_ts": "1791821400.244629",
        "nurture_state": "queued",
        "draft_body": None,
    }
    assert ticker_card_still_open(row) is True
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
    assert cards == []
    kept = memory.get_ticker("t-open-card")
    assert kept["draft_body"] is None
    assert kept["slack_ts"] == "1791821400.244629"
    assert any("card_open" in s for s in report.ticker_skipped)
