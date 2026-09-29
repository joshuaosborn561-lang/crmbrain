from datetime import datetime, timedelta

from crmbrain.config import CDT, STAGE
from crmbrain.cycle import _handle_engagement, apply_gmail_stage_update
from crmbrain.intelligence import extract, heuristic_extract
from crmbrain.memory import Memory
from crmbrain.models import CycleReport, Engagement
from crmbrain.policy import (
    choose_deal_action,
    matching_held_event,
    no_show_write_stage,
    prospect_matches_held,
    resolve_stage,
    scheduled_past_grace,
    should_move_stage,
)
from tests.test_crm_gating import FakeHubSpot, make_settings


SCHEDULED = datetime(2026, 9, 29, 15, 30, tzinfo=CDT)
HELD_AT = datetime(2026, 9, 29, 15, 35, tzinfo=CDT)
CYCLE_5PM = datetime(2026, 9, 29, 17, 0, tzinfo=CDT)


def _tyler_contact():
    return {
        "id": "tyler-1",
        "properties": {
            "email": "tyler@deeprootscapital.com",
            "firstname": "Tyler",
            "lastname": "Leverington",
            "company": "Deep Roots Capital",
            "crm_source": "calendly",
        },
    }


def _scheduled_deal(stage=None):
    return {
        "id": "tyler-deal",
        "contact_id": "tyler-1",
        "properties": {
            "dealstage": stage or STAGE["discovery_scheduled"],
            "dealname": "Tyler Leverington - Deep Roots Capital",
        },
    }


def _fireflies(*, occurred_at=HELD_AT, email="tyler@deeprootscapital.com", extra=None):
    return Engagement(
        source="fireflies",
        external_id="ff-tyler",
        occurred_at=occurred_at,
        email=email,
        first_name="Tyler",
        last_name="Leverington",
        name="Tyler Leverington",
        company="Deep Roots Capital",
        domain="deeprootscapital.com",
        transcript="SalesGlider discovery with Tyler about their capital pipeline.",
        raw_subject="Tyler Leverington and Joshua Osborn",
        extra=extra
        or {
            "participants": ["tyler@deeprootscapital.com", "joshua@salesglidergrowth.com"],
            "meeting_attendees": [
                {"displayName": "Tyler Leverington", "email": email},
                {"displayName": "Joshua Osborn", "email": "joshua@salesglidergrowth.com"},
            ],
        },
    )


def _gmail_no_show(*, scheduled_at=SCHEDULED, already_id="g-noshow"):
    return Engagement(
        source="gmail",
        external_id=already_id,
        occurred_at=CYCLE_5PM,
        email="tyler@deeprootscapital.com",
        first_name="Tyler",
        last_name="Leverington",
        name="Tyler Leverington",
        company="Deep Roots Capital",
        domain="deeprootscapital.com",
        raw_subject="Invitee no-show: Tyler Leverington - SalesGlider Intro",
        summary="Tue Sep 29 2026 3:30PM CDT\nSalesGlider Intro",
        stage_hint=STAGE["no_show"],
        extra={
            "hubspot_contact_id": "tyler-1",
            "meeting_when": "Tue Sep 29 2026 3:30PM CDT",
            "meeting_at": scheduled_at.isoformat(),
            "event_type": "SalesGlider Intro",
        },
    )


def _prep(tmp_path, stage=None, crm_source="calendly"):
    contact = _tyler_contact()
    contact["properties"]["crm_source"] = crm_source
    hs = FakeHubSpot([contact])
    hs.deals.append(_scheduled_deal(stage))
    memory = Memory(make_settings(), data_dir=tmp_path)
    return hs, memory, CycleReport()


def test_same_day_fireflies_blocks_no_show_and_promotes(tmp_path):
    hs, memory, report = _prep(tmp_path)
    held = _fireflies()
    _handle_engagement(held, make_settings(), hs, memory, None, report)
    assert hs.deals[0]["properties"]["dealstage"] == STAGE["discovery_completed"]

    apply_gmail_stage_update(
        _gmail_no_show(),
        make_settings(),
        hs,
        memory,
        None,
        report,
        held_events=[held],
    )
    assert hs.deals[0]["properties"]["dealstage"] == STAGE["discovery_completed"]
    assert not any("no_show" in t for t in report.ticker_enrolled)
    assert not any(STAGE["no_show"] in str(d) for d in report.deals_moved)


def test_late_fireflies_transcript_still_wins(tmp_path):
    hs, memory, report = _prep(tmp_path)
    late = _fireflies(occurred_at=SCHEDULED + timedelta(hours=3))
    assert no_show_write_stage(
        prospect=_gmail_no_show(),
        contact=hs.contacts[0],
        current_stage=STAGE["discovery_scheduled"],
        held_events=[late],
        scheduled_at=SCHEDULED,
        now=CYCLE_5PM + timedelta(hours=2),
    ) == STAGE["discovery_completed"]

    apply_gmail_stage_update(
        _gmail_no_show(),
        make_settings(),
        hs,
        memory,
        None,
        report,
        held_events=[late],
    )
    assert hs.deals[0]["properties"]["dealstage"] == STAGE["discovery_completed"]


def test_true_no_show_without_transcript_still_moves(tmp_path):
    hs, memory, report = _prep(tmp_path)
    past = SCHEDULED - timedelta(hours=4)
    ev = _gmail_no_show(scheduled_at=past)
    assert no_show_write_stage(
        prospect=ev,
        contact=hs.contacts[0],
        current_stage=STAGE["discovery_scheduled"],
        held_events=[],
        scheduled_at=past,
        now=CYCLE_5PM,
    ) == STAGE["no_show"]

    apply_gmail_stage_update(ev, make_settings(), hs, memory, None, report, held_events=[])
    assert hs.deals[0]["properties"]["dealstage"] == STAGE["no_show"]
    assert any("no_show" in t for t in report.ticker_enrolled)


def test_llm_no_show_override_ignored_when_transcript_exists(tmp_path):
    ev = _fireflies()
    ev.transcript = (
        "Discovery call with Tyler. Someone joked they didn't show last week, "
        "but Tyler was on this call the whole time walking pipeline."
    )
    facts = heuristic_extract(ev.transcript)
    assert facts["stage_hint"] == "no_show"
    gemini = {"stage_hint": "no_show", "ticker_reason": "no_show"}
    assert resolve_stage(ev, gemini) == STAGE["discovery_completed"]
    assert choose_deal_action(STAGE["discovery_scheduled"], STAGE["no_show"], ev) == STAGE[
        "discovery_completed"
    ]
    extracted = extract(make_settings(), ev)
    assert extracted["stage_hint"] != "no_show"
    assert extracted.get("ticker_reason") != "no_show"

    hs, memory, report = _prep(tmp_path)
    _handle_engagement(ev, make_settings(), hs, memory, None, report)
    assert hs.deals[0]["properties"]["dealstage"] == STAGE["discovery_completed"]
    assert not any("no_show" in t for t in report.ticker_enrolled)


def test_name_domain_match_when_fireflies_email_missing():
    held = _fireflies(
        email="",
        extra={
            "participants": ["Tyler Leverington", "joshua@salesglidergrowth.com"],
            "meeting_attendees": [{"displayName": "Tyler Leverington", "email": ""}],
        },
    )
    held.email = ""
    prospect = _gmail_no_show()
    contact = _tyler_contact()
    assert prospect_matches_held(held, prospect, contact)
    assert matching_held_event(prospect, contact, [held], SCHEDULED) is held
    assert (
        no_show_write_stage(
            prospect=prospect,
            contact=contact,
            current_stage=STAGE["discovery_scheduled"],
            held_events=[held],
            scheduled_at=SCHEDULED,
            now=CYCLE_5PM,
        )
        == STAGE["discovery_completed"]
    )


def test_stale_processed_no_show_does_not_refire(tmp_path):
    hs, memory, report = _prep(tmp_path, stage=STAGE["discovery_completed"], crm_source="fireflies")
    ev = _gmail_no_show()
    memory.mark_processed(ev.source, ev.external_id, {"subject": ev.raw_subject})
    held = _fireflies()
    apply_gmail_stage_update(
        ev,
        make_settings(),
        hs,
        memory,
        None,
        report,
        held_events=[held],
    )
    assert hs.deals[0]["properties"]["dealstage"] == STAGE["discovery_completed"]
    assert not any(STAGE["no_show"] in str(item) for item in report.deals_moved)

    hs2, memory2, report2 = _prep(tmp_path / "stale", stage=STAGE["discovery_scheduled"])
    stale = _gmail_no_show(already_id="g-stale")
    memory2.mark_processed(stale.source, stale.external_id, {"subject": stale.raw_subject})
    apply_gmail_stage_update(
        stale,
        make_settings(),
        hs2,
        memory2,
        None,
        report2,
        held_events=[],
    )
    assert hs2.deals[0]["properties"]["dealstage"] == STAGE["discovery_scheduled"]
    assert any("stale no_show" in s for s in report2.skipped)


def test_grace_window_and_reschedule_block_no_show():
    prospect = _gmail_no_show()
    contact = _tyler_contact()
    assert not scheduled_past_grace(CYCLE_5PM - timedelta(minutes=30), CYCLE_5PM)
    assert (
        no_show_write_stage(
            prospect=prospect,
            contact=contact,
            current_stage=STAGE["discovery_scheduled"],
            held_events=[],
            scheduled_at=CYCLE_5PM - timedelta(minutes=30),
            now=CYCLE_5PM,
        )
        == ""
    )
    assert (
        no_show_write_stage(
            prospect=prospect,
            contact=contact,
            current_stage=STAGE["discovery_scheduled"],
            held_events=[],
            scheduled_at=SCHEDULED,
            now=CYCLE_5PM,
            has_reschedule=True,
        )
        == ""
    )


def test_policy_never_demotes_held_or_completed_to_no_show():
    gmail = _gmail_no_show()
    held = _fireflies()
    assert not should_move_stage(
        STAGE["discovery_completed"], STAGE["no_show"], back_signal=True
    )
    assert should_move_stage(STAGE["discovery_scheduled"], STAGE["no_show"], back_signal=True)
    assert choose_deal_action(STAGE["discovery_completed"], STAGE["no_show"], gmail) is None
    assert choose_deal_action(STAGE["discovery_scheduled"], STAGE["no_show"], gmail) == STAGE["no_show"]
    assert choose_deal_action(STAGE["discovery_scheduled"], STAGE["no_show"], held) == STAGE[
        "discovery_completed"
    ]
    assert resolve_stage(held, {"stage_hint": "no_show"}) == STAGE["discovery_completed"]


def test_crm_source_fireflies_without_held_event_writes_no_show(tmp_path):
    past = SCHEDULED - timedelta(hours=4)
    hs, memory, report = _prep(tmp_path, crm_source="fireflies")
    ev = _gmail_no_show(scheduled_at=past)
    assert (
        no_show_write_stage(
            prospect=ev,
            contact=hs.contacts[0],
            current_stage=STAGE["discovery_scheduled"],
            held_events=[],
            scheduled_at=past,
            now=CYCLE_5PM,
        )
        == STAGE["no_show"]
    )
    apply_gmail_stage_update(ev, make_settings(), hs, memory, None, report, held_events=[])
    assert hs.deals[0]["properties"]["dealstage"] == STAGE["no_show"]

    hs2, memory2, report2 = _prep(tmp_path / "crm-src-stale", crm_source="fireflies")
    stale = _gmail_no_show(scheduled_at=past, already_id="g-crm-src")
    memory2.mark_processed(stale.source, stale.external_id, {"subject": stale.raw_subject})
    apply_gmail_stage_update(stale, make_settings(), hs2, memory2, None, report2, held_events=[])
    assert hs2.deals[0]["properties"]["dealstage"] == STAGE["discovery_scheduled"]
    assert any("stale no_show" in s for s in report2.skipped)


def test_already_processed_no_show_without_held_stays_no_show(tmp_path):
    hs, memory, report = _prep(tmp_path, stage=STAGE["no_show"])
    ev = _gmail_no_show(scheduled_at=SCHEDULED - timedelta(hours=4))
    memory.mark_processed(ev.source, ev.external_id, {"subject": ev.raw_subject})
    apply_gmail_stage_update(ev, make_settings(), hs, memory, None, report, held_events=[])
    assert hs.deals[0]["properties"]["dealstage"] == STAGE["no_show"]
    assert not any(STAGE["discovery_completed"] in str(item) for item in report.deals_moved)


def test_tim_smith_gmail_does_not_match_tom_smith_gmail():
    held = Engagement(
        source="fireflies",
        external_id="ff-tim",
        occurred_at=HELD_AT,
        email="tim@gmail.com",
        first_name="Tim",
        last_name="Smith",
        name="Tim Smith",
        domain="gmail.com",
        extra={
            "participants": ["tim@gmail.com"],
            "meeting_attendees": [{"displayName": "Tim Smith", "email": "tim@gmail.com"}],
        },
    )
    prospect = Engagement(
        source="gmail",
        external_id="g-tom",
        email="tom@gmail.com",
        first_name="Tom",
        last_name="Smith",
        name="Tom Smith",
        domain="gmail.com",
        extra={"meeting_at": SCHEDULED.isoformat()},
    )
    assert not prospect_matches_held(held, prospect, scheduled_at=SCHEDULED)
    assert matching_held_event(prospect, None, [held], SCHEDULED) is None


def test_same_name_on_josh_domain_is_rejected():
    held = _fireflies(email="tyler@salesglidergrowth.com")
    held.domain = "salesglidergrowth.com"
    held.company = ""
    held.extra = {
        "participants": ["tyler@salesglidergrowth.com"],
        "meeting_attendees": [
            {"displayName": "Tyler Leverington", "email": "tyler@salesglidergrowth.com"}
        ],
    }
    prospect = _gmail_no_show()
    assert not prospect_matches_held(held, prospect, _tyler_contact(), scheduled_at=SCHEDULED)


def test_josh_name_on_held_call_produces_no_match():
    held = Engagement(
        source="fireflies",
        external_id="ff-josh",
        occurred_at=HELD_AT,
        email="joshua@salesglidergrowth.com",
        first_name="Joshua",
        last_name="Osborn",
        name="Joshua Osborn",
        extra={
            "participants": ["Joshua Osborn", "joshua@salesglidergrowth.com"],
            "meeting_attendees": [
                {"displayName": "Joshua Osborn", "email": "joshua@salesglidergrowth.com"}
            ],
        },
    )
    prospect = Engagement(
        source="gmail",
        external_id="g-josh",
        email="joshua@deeprootscapital.com",
        first_name="Joshua",
        last_name="Osborn",
        name="Joshua Osborn",
        extra={"meeting_at": SCHEDULED.isoformat()},
    )
    assert not prospect_matches_held(held, prospect, scheduled_at=SCHEDULED)
    assert matching_held_event(prospect, None, [held], SCHEDULED) is None


def test_paid_only_contact_gmail_no_show_creates_no_deal(tmp_path):
    contact = _tyler_contact()
    hs = FakeHubSpot([contact])
    hs.deals.append(
        {
            "id": "paid-deal",
            "contact_id": "tyler-1",
            "properties": {
                "dealstage": STAGE["paid"],
                "dealname": "Tyler Leverington - Deep Roots Capital",
            },
        }
    )
    memory = Memory(make_settings(), data_dir=tmp_path)
    report = CycleReport()
    ev = _gmail_no_show(scheduled_at=SCHEDULED - timedelta(hours=4))
    apply_gmail_stage_update(ev, make_settings(), hs, memory, None, report, held_events=[])
    assert len(hs.deals) == 1
    assert hs.deals[0]["id"] == "paid-deal"
    assert hs.deals[0]["properties"]["dealstage"] == STAGE["paid"]
