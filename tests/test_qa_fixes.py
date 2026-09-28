from datetime import datetime, timedelta, timezone

from crmbrain.calendar_events import (
    attendees_from_gcal_event,
    attendees_from_ics,
    contact_on_calendar,
    keep_event_attendees,
)
from crmbrain.config import (
    CDT,
    STAGE,
    compute_lookback_start,
    gmail_after_clause,
    lookback_dates_cdt,
)
from crmbrain.cycle import _handle_engagement
from crmbrain.memory import Memory
from crmbrain.models import CycleReport, Engagement
from crmbrain.names import (
    format_deal_name,
    is_confident_person_name,
    looks_like_meeting_title,
    name_from_email_local,
    person_name_from_attendee,
    prefer_contact_name,
)
from crmbrain.policy import (
    deal_name_for,
    deal_richness,
    duplicate_open_deal_pairs,
    live_open_deals,
)
from crmbrain.prune import (
    has_live_meeting_evidence,
    prune_notetaker_contacts,
    prune_replied_deals,
)
from crmbrain.sources.fireflies import counterpart_from_fireflies
from crmbrain.sources.gmail_scan import is_notetaker_contact, is_notetaker_email, mail_queries
from tests.test_crm_gating import FakeHubSpot, make_settings


def test_gcal_attendee_counts_as_meeting_and_blocks_prune(tmp_path):
    """Direct Google Calendar invite (not Calendly) must keep the booked contact."""
    contact = {
        "id": "brian-1",
        "properties": {
            "email": "bdonigan@wtrenovations.com",
            "firstname": "Brian",
            "lastname": "Donigan",
            "company": "WT Renovations",
            "crm_source": "smartlead",
        },
    }
    hs = FakeHubSpot([contact])
    hs.deals.append(
        {
            "id": "brian-replied",
            "contact_id": "brian-1",
            "properties": {"dealstage": STAGE["replied"], "dealname": "Brian Donigan -"},
        }
    )
    assert not has_live_meeting_evidence(hs, contact, hs.deals)
    hs.scheduled_attendee_emails = {"bdonigan@wtrenovations.com"}
    assert has_live_meeting_evidence(hs, contact, hs.deals)

    report = CycleReport()
    prune_replied_deals(hs, report)
    assert any(d["id"] == "brian-replied" for d in hs.deals)
    assert hs.deals[0]["properties"]["dealstage"] == STAGE["discovery_scheduled"]
    assert any(c["id"] == "brian-1" for c in hs.contacts)
    assert not any(w[0] == "archive_contact" for w in hs.writes)

    ev = Engagement(
        source="smartlead",
        external_id="sl-brian",
        email="bdonigan@wtrenovations.com",
        first_name="Brian",
        last_name="Donigan",
        summary="Positive SmartLead reply (Interested)",
    )
    memory = Memory(make_settings(), data_dir=tmp_path)
    handle_report = CycleReport()
    _handle_engagement(ev, make_settings(), hs, memory, None, handle_report)
    assert not any(w[0] == "archive_contact" and w[1] == "brian-1" for w in hs.writes)
    assert any(c["id"] == "brian-1" for c in hs.contacts)


def test_ics_and_gcal_event_attendees():
    ics = """
BEGIN:VCALENDAR
BEGIN:VEVENT
SUMMARY:Call with Brian
ORGANIZER;CN=Joshua Osborn:mailto:joshua@salesglidergrowth.com
ATTENDEE;CN=Brian Donigan;RSVP=TRUE:mailto:bdonigan@wtrenovations.com
ATTENDEE;CN=Fireflies Notetaker:mailto:fred@fireflies.ai
END:VEVENT
END:VCALENDAR
"""
    assert attendees_from_ics(ics) == {"bdonigan@wtrenovations.com"}
    event = {
        "status": "confirmed",
        "summary": "Call with Brian",
        "organizer": {"email": "joshua@salesglidergrowth.com"},
        "attendees": [
            {"email": "bdonigan@wtrenovations.com", "responseStatus": "accepted"},
            {"email": "fred@fireflies.ai", "responseStatus": "accepted"},
        ],
    }
    assert attendees_from_gcal_event(event) == {"bdonigan@wtrenovations.com"}
    cancelled = dict(event, status="cancelled")
    assert attendees_from_gcal_event(cancelled) == set()
    assert keep_event_attendees("Weekly pipeline", {"joshua@salesglidergrowth.com"}) == set()
    assert contact_on_calendar("BDonigan@wtrenovations.com", {"bdonigan@wtrenovations.com"})
    assert not contact_on_calendar("other@x.com", {"bdonigan@wtrenovations.com"})


def test_upsert_reuses_workflow_deal_and_dedupes_same_stage():
    hs = FakeHubSpot(
        [
            {
                "id": "c1",
                "properties": {
                    "email": "pat@acme.com",
                    "firstname": "Pat",
                    "lastname": "Lee",
                    "company": "Acme",
                },
            }
        ]
    )
    hs.deals = [
        {
            "id": "workflow",
            "contact_id": "c1",
            "properties": {
                "dealstage": STAGE["discovery_scheduled"],
                "dealname": "Pat Lee -",
                "amount": "",
            },
        },
        {
            "id": "crmbrain",
            "contact_id": "c1",
            "properties": {
                "dealstage": STAGE["discovery_scheduled"],
                "dealname": "Pat Lee - Acme",
                "amount": "",
            },
        },
    ]
    keep, dump = duplicate_open_deal_pairs(hs.deals)[0]
    assert keep["id"] == "crmbrain"
    assert dump["id"] == "workflow"
    assert deal_richness(hs.deals[1]) > deal_richness(hs.deals[0])

    ev = Engagement(
        source="calendly",
        external_id="cal-dup",
        email="pat@acme.com",
        first_name="Pat",
        last_name="Lee",
        company="Acme",
    )
    deal = hs.upsert_deal(hs.contacts[0], ev, STAGE["discovery_scheduled"])
    assert deal["id"] == "crmbrain"
    assert {d["id"] for d in hs.deals} == {"crmbrain"}
    assert any(w[0] == "archive_deal" and w[1] == "workflow" for w in hs.writes)
    assert len(live_open_deals(hs.deals)) == 1


def test_upsert_reuses_just_created_open_deal_instead_of_second():
    hs = FakeHubSpot(
        [{"id": "c2", "properties": {"email": "a@b.com", "firstname": "Ann", "lastname": "Oak"}}]
    )
    hs.deals.append(
        {
            "id": "just-created",
            "contact_id": "c2",
            "properties": {"dealstage": STAGE["discovery_scheduled"], "dealname": "Ann Oak -"},
        }
    )
    ev = Engagement(
        source="calendly",
        external_id="cal-2",
        first_name="Ann",
        last_name="Oak",
        email="a@b.com",
        company="Oak Co",
    )
    deal = hs.upsert_deal(hs.contacts[0], ev, STAGE["discovery_scheduled"])
    assert deal["id"] == "just-created"
    assert len(hs.deals) == 1
    assert hs.deals[0]["properties"]["dealname"] == "Ann Oak - Oak Co"


def test_meeting_title_is_never_a_person_or_deal_name():
    title = "Terrell and Bob — MSP Staffing & PE Acquisition Strategy"
    assert looks_like_meeting_title(title)
    assert looks_like_meeting_title("Brian Donigan (Google Calendar)")
    assert not looks_like_meeting_title("Brian Donigan")
    assert not is_confident_person_name(title)
    assert person_name_from_attendee(title, "bdonigan@wtrenovations.com") == ("", "")
    assert name_from_email_local("bdonigan@wtrenovations.com") == ("", "")
    assert name_from_email_local("brian.donigan@wtrenovations.com") == ("Brian", "Donigan")
    assert person_name_from_attendee("Brian Donigan", "bdonigan@x.com") == ("Brian", "Donigan")
    assert prefer_contact_name("Brian", title) == "Brian"
    assert prefer_contact_name(title, "Brian") == "Brian"
    assert prefer_contact_name("Brian", "Robert") == "Brian"
    assert format_deal_name("Brian", "Donigan", "WT Renovations") == "Brian Donigan - WT Renovations"
    ev = Engagement(
        source="fireflies",
        external_id="ff-title",
        name=title,
        email="guest@example.com",
        company="Example",
    )
    assert ev.display_name() == ""
    assert deal_name_for(ev) == "Example"


def test_fireflies_uses_attendee_not_meeting_title():
    title = "Terrell and Bob — MSP Staffing & PE Acquisition Strategy"
    name, first, last, email = counterpart_from_fireflies(
        title,
        ["joshua@salesglidergrowth.com", "Brian Donigan <bdonigan@wtrenovations.com>"],
        [{"displayName": "Brian Donigan", "email": "bdonigan@wtrenovations.com"}],
    )
    assert email == "bdonigan@wtrenovations.com"
    assert first == "Brian"
    assert last == "Donigan"
    assert name == "Brian Donigan"
    assert title not in name
    blank_name, first2, last2, email2 = counterpart_from_fireflies(
        title,
        ["bdonigan@wtrenovations.com"],
        [],
    )
    assert email2 == "bdonigan@wtrenovations.com"
    assert blank_name == ""
    assert first2 == ""
    assert last2 == ""


def test_fireflies_title_does_not_overwrite_good_hubspot_name(tmp_path):
    title = "Terrell and Bob — MSP Staffing & PE Acquisition Strategy"
    ev = Engagement(
        source="fireflies",
        external_id="ff-bad-title",
        name=title,
        email="bdonigan@wtrenovations.com",
        transcript="Discovery with Brian about renovations.",
        raw_subject=title,
    )
    hs = FakeHubSpot(
        [
            {
                "id": "b1",
                "properties": {
                    "email": "bdonigan@wtrenovations.com",
                    "firstname": "Brian",
                    "lastname": "Donigan",
                    "company": "WT Renovations",
                    "crm_source": "calendly",
                },
            }
        ]
    )
    memory = Memory(make_settings(), data_dir=tmp_path)
    _handle_engagement(ev, make_settings(), hs, memory, None, CycleReport())
    props = hs.contacts[0]["properties"]
    assert props["firstname"] == "Brian"
    assert props["lastname"] == "Donigan"
    assert title not in (props["firstname"] + " " + props["lastname"])
    assert hs.deals[0]["properties"]["dealname"] == "Brian Donigan - WT Renovations"


def test_monday_lookback_covers_friday_evening():
    friday_5pm = datetime(2026, 9, 25, 17, 0, tzinfo=CDT)
    monday_7am = datetime(2026, 9, 28, 7, 0, tzinfo=CDT)
    settings = make_settings()
    fallback = compute_lookback_start(settings, None, now=monday_7am)
    assert fallback == monday_7am.astimezone(timezone.utc) - timedelta(hours=36)
    # Fixed 36h from Monday 7am CT starts Friday 7pm — Friday 5-7pm is missing.
    friday_6pm = datetime(2026, 9, 25, 18, 0, tzinfo=CDT)
    assert friday_6pm.astimezone(timezone.utc) < fallback

    start = compute_lookback_start(settings, friday_5pm, now=monday_7am)
    assert start <= datetime(2026, 9, 25, 15, 0, tzinfo=CDT).astimezone(timezone.utc)
    assert start < friday_6pm.astimezone(timezone.utc)
    windowed = make_settings()
    from dataclasses import replace

    windowed = replace(windowed, lookback_start_at=start)
    after = gmail_after_clause(start)
    assert after == "after:2026/09/25"
    assert all(after in q for q in mail_queries(windowed))
    dates = lookback_dates_cdt(windowed)
    assert "2026-09-25" in dates
    assert "2026-09-28" in dates


def test_lookback_falls_back_without_prior_run_and_caps():
    now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    settings = make_settings(lookback_hours=36)
    assert compute_lookback_start(settings, None, now=now) == now - timedelta(hours=36)
    ancient = datetime(2026, 8, 1, tzinfo=timezone.utc)
    capped = compute_lookback_start(settings, ancient, now=now)
    assert (now - capped).total_seconds() / 3600 == 24 * 7


def test_notetaker_archived_in_any_stage():
    hs = FakeHubSpot(
        [
            {
                "id": "fred",
                "properties": {
                    "email": "fred@fireflies.ai",
                    "firstname": "Fireflies",
                    "lastname": "Notetaker",
                    "crm_source": "fireflies",
                },
            }
        ]
    )
    hs.deals.append(
        {
            "id": "fred-live",
            "contact_id": "fred",
            "properties": {
                "dealstage": STAGE["discovery_completed"],
                "dealname": "Fireflies Notetaker",
            },
        }
    )
    assert is_notetaker_email("fred@fireflies.ai")
    assert is_notetaker_contact(hs.contacts[0])
    report = CycleReport()
    prune_notetaker_contacts(hs, report)
    assert hs.deals == []
    assert hs.contacts == []
    assert any(w[0] == "archive_deal" and w[1] == "fred-live" for w in hs.writes)
    assert any(w[0] == "archive_contact" and w[1] == "fred" for w in hs.writes)
    assert any("fred@fireflies.ai" in x for x in report.contacts_pruned)


def test_memory_reads_last_finished_cycle_start(tmp_path):
    memory = Memory(make_settings(), data_dir=tmp_path)
    memory._local["runs"] = [
        {"status": "ok", "started_at": "2026-09-25T22:00:00+00:00", "report": {}},
        {"status": "running", "started_at": "2026-09-28T12:00:00+00:00", "report": {}},
    ]
    stamp = memory.last_finished_run_started_at()
    assert stamp == datetime(2026, 9, 25, 22, 0, tzinfo=timezone.utc)
