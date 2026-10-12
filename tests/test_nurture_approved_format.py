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


def _robert_row() -> dict:
    recap = (
        "Cold calling was carrying the load and cold email was getting caught in spam filters. "
        "Robert was taking it to Steve for a budget call in early October."
    )
    return {
        "name": "Robert Lawson",
        "email": "rob@cyberguard360.com",
        "company": "CyberGuard360",
        "industry": "",
        "reason": "met",
        "meeting_source": "fireflies",
        "fireflies": True,
        "fireflies_id": "ff-robert-aug28",
        "meeting_at": "2026-08-28T17:00:00+00:00",
        "summary": recap,
        "last_touch_snippet": recap,
    }


def _laura_row() -> dict:
    recap = (
        "Getting new clients was the bottleneck. It would take at least 1 interested meeting "
        "a week to make outreach worth it for GRN."
    )
    return {
        "name": "Laura Klein",
        "email": "lklein@grnplano.com",
        "company": "GRN Plano",
        "industry": "",
        "reason": "booked",
        "meeting_at": "2026-09-03T18:00:00+00:00",
        "summary": "",
        "last_touch_snippet": "",
        "extra": {
            "notes": (
                "Healthcare recruiting / nurse staffing. " + recap
            ),
        },
    }


def _brian_row() -> dict:
    recap = (
        "Commercial roofing to property owners and managers within 2-3h of KC, not GCs. "
        "Team closes about 35%. Partner Danny. Revisit in February."
    )
    return {
        "name": "Brian Donigan",
        "email": "bdonigan@wtrenovations.com",
        "company": "WT Renovations",
        "industry": "construction",
        "reason": "met",
        "meeting_source": "fireflies",
        "fireflies": True,
        "fireflies_id": "ff-brian-sep25",
        "meeting_at": "2026-09-25T19:00:00+00:00",
        "summary": recap,
        "last_touch_snippet": recap,
    }


def test_robert_lawson_approved_held_recap_and_cyber_proof():
    draft = compose_nurture_draft(_robert_row())
    opener = _opener_block(draft.body)
    _assert_complete_sentences(opener.split("\n\n", 1)[-1])
    assert draft.valid is True
    assert draft.subject == "CyberGuard360 follow up"
    assert draft.body.startswith("Hey Robert,")
    assert "aug" in opener.lower() or "august" in opener.lower()
    assert "cold calling" in opener.lower()
    assert "spam" in opener.lower()
    assert "steve" in opener.lower()
    assert "meeting we had booked" not in draft.body.lower()
    assert LANE_PROOF_BANK["msp"] in draft.body
    assert "Where did you and Steve land?" in draft.body
    assert MEETING_GUARANTEE in draft.body
    assert draft.body.strip().endswith("Josh Osborn")
    assert not opener.rstrip().endswith((",", ";", " and"))


def test_laura_klein_blank_industry_is_staffing():
    draft = compose_nurture_draft(_laura_row())
    opener = _opener_block(draft.body)
    _assert_complete_sentences(opener.split("\n\n", 1)[-1])
    assert draft.valid is True
    assert draft.subject == "GRN Plano follow up"
    assert draft.body.startswith("Hey Laura,")
    assert "sep" in opener.lower() or "september" in opener.lower()
    assert "new clients" in opener.lower()
    assert "1" in opener
    assert "meeting we had booked" not in draft.body.lower()
    assert LANE_PROOF_BANK["staffing"] in draft.body
    assert "Is finding new clients still the main thing on your plate?" in draft.body
    assert MEETING_GUARANTEE in draft.body


def _justin_labeled_row() -> dict:
    note = (
        "Last real conversation (late Jul/Aug): Walked cold outbound and using AI agents "
        "to find new clients for Only PT Recruiting."
    )
    return {
        "name": "Justin Bechtel",
        "email": "justin@onlyptrecruiting.com",
        "company": "Only PT Recruiting",
        "industry": "",
        "reason": "met",
        "meeting_source": "fireflies",
        "fireflies": True,
        "fireflies_id": "ff-justin-jul30",
        "fireflies_at": "2026-07-30T16:00:00+00:00",
        "held_at": "2026-07-30T16:00:00+00:00",
        "meeting_at": "2026-07-30T16:00:00+00:00",
        "summary": "",
        "last_touch_snippet": note,
        "draft_body": (
            "Hey Justin, you mentioned Last real conversation (late Jul/Aug): Walked cold outbound.\n\n"
            f"{LANE_PROOF_BANK['staffing']}\n\n"
            f"{NURTURE_QUESTION}\n\n"
            f"{MEETING_GUARANTEE}\n\n"
            "Josh Osborn"
        ),
    }


def _vincent_row() -> dict:
    recap = (
        "Discussed getting beyond HCM, PEO and payroll so the firm is not riding a few "
        "big accounts. Bringing on junior recruiters for new verticals."
    )
    return {
        "name": "Vincent Bowden",
        "email": "vbowden@talentunify.com",
        "company": "TalentUnify",
        "industry": "",
        "reason": "met",
        "meeting_source": "fireflies",
        "fireflies": True,
        "fireflies_id": "ff-vincent-sep2",
        "meeting_at": "2026-09-02T17:00:00+00:00",
        "summary": recap,
        "last_touch_snippet": recap,
        "draft_subject": "Talentunify follow up",
        "draft_body": (
            "Hey Vincent, following up on our Sep 2 call.\n\n"
            f"{LANE_PROOF_BANK['staffing']}\n\n"
            f"{NURTURE_QUESTION}\n\n"
            f"{MEETING_GUARANTEE}\n\n"
            "Josh Osborn"
        ),
    }


def test_brian_donigan_construction_plus_roofing_recap():
    draft = compose_nurture_draft(_brian_row())
    opener = _opener_block(draft.body)
    _assert_complete_sentences(opener.split("\n\n", 1)[-1])
    assert draft.valid is True
    assert "sep 25" in opener.lower() or "sep" in opener.lower()
    assert "commercial roofing" in opener.lower()
    assert "property" in opener.lower()
    assert "meeting we had booked" not in draft.body.lower()
    assert LANE_PROOF_BANK["roofing"] in draft.body
    assert "6 property managers" in draft.body
    assert "february" in draft.body.lower()
    assert MEETING_GUARANTEE in draft.body
    assert not opener.rstrip().endswith((",", ";", " and"))


def test_justin_bechtel_strips_labeled_note_and_does_not_paste_it():
    row = _justin_labeled_row()
    draft = compose_nurture_draft(row)
    opener = _opener_block(draft.body)
    _assert_complete_sentences(opener.split("\n\n", 1)[-1])
    assert draft.valid is True
    assert draft.body.startswith("Hey Justin,")
    assert "last real conversation" not in draft.body.lower()
    assert "jul/aug" not in draft.body.lower()
    assert "you mentioned last" not in draft.body.lower()
    assert row["draft_body"].split("\n", 1)[0] not in draft.body
    assert "cold outbound" in opener.lower() or "ai agents" in opener.lower()
    assert "new clients" in opener.lower()
    assert LANE_PROOF_BANK["staffing"] in draft.body
    assert "meeting we had booked" not in draft.body.lower()
    assert not opener.rstrip().endswith((",", ";", " and"))


def test_vincent_bowden_uses_recap_not_generic_sep_2_followup():
    row = _vincent_row()
    draft = compose_nurture_draft(row)
    opener = _opener_block(draft.body)
    _assert_complete_sentences(opener.split("\n\n", 1)[-1])
    assert draft.valid is True
    assert draft.subject == "TalentUnify follow up"
    assert "talentunify follow up" != draft.subject.lower() or draft.subject == "TalentUnify follow up"
    assert "following up on our sep 2 call" not in draft.body.lower()
    assert "following up on our" not in opener.lower()
    assert row["draft_body"].split("\n", 1)[0] not in draft.body
    assert "hcm" in opener.lower() or "peo" in opener.lower() or "payroll" in opener.lower()
    assert "recruiter" in opener.lower() or "vertical" in opener.lower()
    assert LANE_PROOF_BANK["staffing"] in draft.body
    assert MEETING_GUARANTEE in draft.body


def test_subjects_keep_stored_company_capitalization():
    robert = compose_nurture_draft(_robert_row())
    vincent = compose_nurture_draft(_vincent_row())
    assert robert.subject == "CyberGuard360 follow up"
    assert "Cyberguard360" not in robert.subject
    assert vincent.subject == "TalentUnify follow up"
    assert "Talentunify" not in vincent.subject
    assert resolve_nurture_company("CyberGuard360", "", "rob@cyberguard360.com") == "CyberGuard360"
    assert resolve_nurture_company("TalentUnify", "", "vbowden@talentunify.com") == "TalentUnify"
    assert (
        resolve_nurture_company("", "robert Lawson - CyberGuard360", "rob@cyberguard360.com")
        == "CyberGuard360"
    )


def test_robert_opener_comes_from_recap_not_stored_draft_body():
    stored = (
        "Hey Robert, this stored draft should never be copied into the opener.\n\n"
        f"{LANE_PROOF_BANK['msp']}\n\n"
        "Where did you and Steve land?\n\n"
        f"{MEETING_GUARANTEE}\n\n"
        "Josh Osborn"
    )
    row = {
        **_robert_row(),
        "draft_subject": "Cyberguard360 follow up",
        "draft_body": stored,
    }
    draft = compose_nurture_draft(row)
    opener = _opener_block(draft.body)
    assert draft.subject == "CyberGuard360 follow up"
    assert stored.split("\n", 1)[0] not in draft.body
    assert "this stored draft should never" not in draft.body
    assert "cold calling" in opener.lower()
    assert "spam" in opener.lower()
    assert draft.valid is True


def test_laura_staffing_from_extra_terms_when_industry_blank():
    draft = compose_nurture_draft(_laura_row())
    assert draft.reject_reason != "no_lane_proof"
    assert LANE_PROOF_BANK["staffing"] in draft.body
    opener = _opener_block(draft.body)
    assert opener.lower() != "hey laura,"
    assert "new clients" in opener.lower() or "interested meeting" in opener.lower()
