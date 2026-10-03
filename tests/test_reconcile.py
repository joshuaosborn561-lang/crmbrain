"""Holistic accuracy: the Sep 2026 misses and the non-deal cases."""

from datetime import datetime, timedelta, timezone

from crmbrain.calendar_events import classify_gcal_event, may_create_contact_from_event
from crmbrain.config import CDT, STAGE
from crmbrain.cycle import _handle_engagement, _in_window
from crmbrain.documents import looks_free_document, stage_from_signature_mail
from crmbrain.evidence import KIND_POC, build_timelines, kind_for, reply_only
from crmbrain.intent import classify, heuristic_intent, is_confident_non_sales, is_confident_sales
from crmbrain.memory import Memory
from crmbrain.models import CycleReport, Engagement
from crmbrain.policy import choose_deal_action, resolve_stage
from crmbrain.reconcile import apply_timeline, reeval_discovery_scheduled, restore_missing_deals
from crmbrain.sources.allo import _headers, _usable_call, row_to_engagement
from crmbrain.staleness import business_days_between, is_stale
from tests.test_crm_gating import FakeHubSpot, make_settings
from tests.test_qa_fixes import _gcal_event


def _now():
    return datetime(2026, 9, 30, 18, 0, tzinfo=timezone.utc)


def test_tyler_poc_hint_goes_to_review_not_signed(tmp_path):
    ev = Engagement(
        source="fireflies",
        external_id="ff-tyler-poc",
        email="tyler@deeprootscapital.com",
        first_name="Tyler",
        last_name="Leverington",
        company="Deep Roots Capital",
        transcript="Tyler and Josh kicked off the paid POC. Onboarding starts this week.",
        raw_subject="Tyler Leverington POC kickoff",
    )
    assert kind_for(ev) == KIND_POC
    assert resolve_stage(ev) == STAGE["discovery_completed"]
    hs = FakeHubSpot(
        [
            {
                "id": "tyler-1",
                "properties": {
                    "email": "tyler@deeprootscapital.com",
                    "firstname": "Tyler",
                    "lastname": "Leverington",
                    "company": "Deep Roots Capital",
                    "crm_source": "calendly",
                },
            }
        ]
    )
    hs.deals.append(
        {
            "id": "tyler-deal",
            "contact_id": "tyler-1",
            "properties": {
                "dealstage": STAGE["discovery_completed"],
                "dealname": "Tyler Leverington - Deep Roots Capital",
                "amount": "5000",
            },
        }
    )
    report = CycleReport()
    apply_timeline(
        build_timelines([ev])["email:tyler@deeprootscapital.com"],
        make_settings(),
        hs,
        Memory(make_settings(), data_dir=tmp_path),
        report,
    )
    assert hs.deals[0]["properties"]["dealstage"] == STAGE["discovery_completed"]
    assert hs.deals[0]["properties"]["amount"] == "5000"
    assert any("poc_hint" in x for x in report.review_queue)


def test_boyd_cold_email_future_invite_creates_even_outside_lookback():
    start = _now() + timedelta(days=6)
    event, now = _gcal_event(
        summary="SalesGlider Boyd Cold Email",
        start=start,
        attendees=[
            {"email": "joshua@salesglidergrowth.com", "responseStatus": "accepted", "self": True},
            {"email": "jboyd@boydsoftx.com", "responseStatus": "accepted"},
        ],
    )
    classified = classify_gcal_event(event, now=now)
    assert classified.upcoming
    assert may_create_contact_from_event(classified)
    ev = Engagement(
        source="calendly",
        external_id="gcal:boyd",
        occurred_at=start,
        email="jboyd@boydsoftx.com",
        first_name="John",
        last_name="Boyd",
        raw_subject="SalesGlider Boyd Cold Email",
        extra={"gcal_create": True, "create_new": True, "skip_lookback": True},
    )
    settings = make_settings()
    from dataclasses import replace

    settings = replace(settings, lookback_start_at=_now() - timedelta(hours=36))
    assert _in_window(ev, settings)
    assert is_confident_sales(heuristic_intent(ev))


def test_cold_email_title_is_sales_not_rejected():
    event, now = _gcal_event(summary="SalesGlider Boyd Cold Email")
    assert may_create_contact_from_event(classify_gcal_event(event, now=now))


def test_destiny_reconcile_restores_deal_when_evidence_and_none_open(tmp_path):
    ev_cal = Engagement(
        source="calendly",
        external_id="cal-destiny",
        email="destiny@mackeymitchell.com",
        first_name="Destiny",
        last_name="Silva",
        company="Mackey Mitchell",
        raw_subject="SalesGlider Intro - Destiny Silva",
        extra={"event_type": "SalesGlider Intro"},
    )
    ev_ff = Engagement(
        source="fireflies",
        external_id="ff-destiny",
        email="destiny@mackeymitchell.com",
        first_name="Destiny",
        last_name="Silva",
        transcript="Discovery with Destiny about their architecture pipeline.",
        raw_subject="Destiny Silva and Joshua Osborn",
    )
    hs = FakeHubSpot(
        [
            {
                "id": "des-1",
                "properties": {
                    "email": "destiny@mackeymitchell.com",
                    "firstname": "Destiny",
                    "lastname": "Silva",
                    "company": "Mackey Mitchell",
                    "crm_source": "fireflies",
                },
            }
        ]
    )
    assert hs.deals == []
    timelines = build_timelines([ev_cal, ev_ff])
    timelines["email:destiny@mackeymitchell.com"].contact = hs.contacts[0]
    report = CycleReport()
    restore_missing_deals(
        hs, make_settings(), Memory(make_settings(), data_dir=tmp_path), report, timelines
    )
    assert hs.deals
    assert hs.deals[0]["properties"]["dealstage"] == STAGE["discovery_completed"]
    assert report.deals_restored


def test_goliath_free_sow_is_not_signed_viewed_agreement_is_proposal():
    assert looks_free_document("Document completed", "The Free SOW has been completed", "Free SOW")
    stage, amount, name = stage_from_signature_mail(
        "Document completed",
        "PandaDoc <noreply@pandadoc.com>",
        "has been completed",
        "The Free SOW has been signed. No charge.",
    )
    assert stage == ""
    viewed, viewed_amt, viewed_name = stage_from_signature_mail(
        "Dave Ackley viewed Growth Partners Agreement",
        "PandaDoc <noreply@pandadoc.com>",
        "viewed the document",
        "Dave Ackley viewed Growth Partners Agreement. Investment $21,000.",
    )
    assert viewed == STAGE["proposal_sent"]
    assert viewed_amt == "21000"
    ev = Engagement(
        source="gmail",
        external_id="pd-goliath",
        email="dave@goliath.com",
        first_name="Dave",
        last_name="Ackley",
        company="Goliath",
        raw_subject="Dave Ackley viewed Growth Partners Agreement",
        stage_hint=STAGE["proposal_sent"],
        extra={"document_name": "Growth Partners Agreement", "amount": "21000"},
    )
    assert choose_deal_action(STAGE["signed"], STAGE["proposal_sent"], ev) is None
    matched = {
        "id": "g1",
        "properties": {"dealstage": STAGE["signed"], "document_name": "Growth Partners Agreement"},
    }
    assert choose_deal_action(STAGE["signed"], STAGE["proposal_sent"], ev, deal=matched) is None


def test_allo_uses_api_key_scheme_and_skips_voicemail_blasts():
    settings = make_settings(allo_key="ak_live_test", allo_url="https://api.withallo.com")
    assert _headers(settings)["Authorization"] == "Api-Key ak_live_test"
    assert not _usable_call({"duration": 4, "result": "CLOSED", "summary": "", "transcript": []})
    row = {
        "id": "cll-1",
        "direction": "INBOUND",
        "contact_number": "+15551212",
        "contact_name": "Pat Lee",
        "duration": 180,
        "result": "ANSWERED",
        "summary": "Pat asked about the SalesGlider retainer and a discovery next week.",
        "transcript": [{"text": "Let's book a SalesGlider intro.", "source": "USER"}],
        "date": "2026-09-30T15:00:00Z",
    }
    assert _usable_call(row)
    ev = row_to_engagement(row)
    assert ev.source == "allo"
    assert ev.phone == "+15551212"


def test_discovery_scheduled_reeval_noshow_completed_nurture(tmp_path):
    from datetime import datetime, timedelta, timezone

    past = datetime.now(timezone.utc) - timedelta(hours=5)
    hs = FakeHubSpot(
        [
            {
                "id": "past-1",
                "properties": {"email": "past@x.com", "firstname": "Past", "lastname": "Lead"},
            },
            {
                "id": "held-1",
                "properties": {
                    "email": "held@x.com",
                    "firstname": "Held",
                    "lastname": "Lead",
                    "crm_source": "fireflies",
                },
            },
            {
                "id": "cx-1",
                "properties": {"email": "cx@x.com", "firstname": "Cancel", "lastname": "Lead"},
            },
            {
                "id": "unk-1",
                "properties": {"email": "unk@x.com", "firstname": "Unknown", "lastname": "Time"},
            },
        ]
    )
    hs.deals = [
        {"id": "d-past", "contact_id": "past-1", "properties": {"dealstage": STAGE["discovery_scheduled"], "dealname": "Past"}},
        {"id": "d-held", "contact_id": "held-1", "properties": {"dealstage": STAGE["discovery_scheduled"], "dealname": "Held"}},
        {"id": "d-cx", "contact_id": "cx-1", "properties": {"dealstage": STAGE["discovery_scheduled"], "dealname": "Cancel"}},
        {"id": "d-unk", "contact_id": "unk-1", "properties": {"dealstage": STAGE["discovery_scheduled"], "dealname": "Unknown"}},
    ]
    canceled = Engagement(
        source="calendly",
        external_id="cx",
        email="cx@x.com",
        first_name="Cancel",
        last_name="Lead",
        extra={"canceled": True},
        raw_subject="Canceled: SalesGlider Intro",
    )
    past_booked = Engagement(
        source="calendly",
        external_id="past",
        email="past@x.com",
        first_name="Past",
        last_name="Lead",
        extra={"meeting_at": past.isoformat()},
        raw_subject="SalesGlider Intro",
    )
    held = Engagement(
        source="fireflies",
        external_id="ff-held",
        email="held@x.com",
        first_name="Held",
        last_name="Lead",
        occurred_at=past + timedelta(minutes=10),
        extra={"meeting_attendees": [{"email": "held@x.com", "name": "Held Lead"}]},
        transcript="Discovery held with Held Lead.",
        raw_subject="Held Lead and Joshua Osborn",
    )
    timelines = build_timelines([canceled, past_booked, held])
    report = CycleReport()
    reeval_discovery_scheduled(
        hs,
        make_settings(),
        Memory(make_settings(), data_dir=tmp_path),
        report,
        timelines,
        upcoming_emails=set(),
        held_events=[held],
        calendar_api_ok=True,
    )
    by_id = {d["id"]: d["properties"]["dealstage"] for d in hs.deals}
    assert by_id["d-past"] == STAGE["discovery_scheduled"]
    assert hs.deals[0]["properties"].get("no_show_count") == "1" or any(
        (d.get("id") == "d-past" and (d.get("properties") or {}).get("no_show_count") == "1")
        for d in hs.deals
    )
    assert by_id["d-held"] == STAGE["discovery_completed"]
    assert by_id["d-cx"] == STAGE["nurture"]
    assert by_id["d-unk"] == STAGE["discovery_scheduled"]
    assert any("unknown_scheduled_time" in x for x in report.review_queue)


def test_boyd_gibbons_reply_only_is_not_a_booked_meeting(tmp_path):
    ev = Engagement(
        source="heyreach",
        external_id="hr-boyd",
        first_name="Boyd",
        last_name="Gibbons",
        linkedin_url="https://www.linkedin.com/in/boydgibbons",
        summary="Interested",
    )
    assert reply_only(build_timelines([ev])["name:boyd gibbons"])
    assert is_confident_sales(heuristic_intent(ev))
    hs = FakeHubSpot(
        [{"id": "bg-1", "properties": {"email": "", "firstname": "Boyd", "lastname": "Gibbons"}}]
    )
    hs.deals.append(
        {
            "id": "bg-deal",
            "contact_id": "bg-1",
            "properties": {"dealstage": STAGE["discovery_scheduled"], "dealname": "Boyd Gibbons"},
        }
    )
    timeline = build_timelines([ev])["name:boyd gibbons"]
    timeline.contact = hs.contacts[0]
    timeline.deals = list(hs.deals)
    report = CycleReport()
    apply_timeline(timeline, make_settings(), hs, Memory(make_settings(), data_dir=tmp_path), report)
    assert hs.deals == []
    assert any("reply-only" in x for x in report.deals_pruned)


def test_non_deals_cynthia_alex_mentor_meraki_stay_out(tmp_path):
    cases = [
        Engagement(
            source="fireflies",
            external_id="cynthia",
            name="Cynthia Hernandez",
            first_name="Cynthia",
            last_name="Hernandez",
            email="cynthia@chorbie.com",
            raw_subject="Marketing Masterclass with Cynthia Hernandez",
            transcript="Josh asked Cynthia about her marketing masterclass and Chorbie.",
        ),
        Engagement(
            source="calendly",
            external_id="alex",
            name="Alex Branning",
            first_name="Alex",
            last_name="Branning",
            email="alex@example.com",
            raw_subject="Alex Branning",
            extra={"event_type": "Alex Branning"},
        ),
        Engagement(
            source="fireflies",
            external_id="mark",
            name="Mark",
            raw_subject="Mark/Josh weekly",
            transcript="Mentor catch-up. No SalesGlider pitch.",
        ),
        Engagement(
            source="fireflies",
            external_id="meraki",
            name="Meraki Discussion",
            email="coworker@insight.com",
            raw_subject="Meraki Discussion",
            transcript="Insight.com Cisco Meraki day-job discussion.",
        ),
    ]
    for ev in cases:
        decision = classify(make_settings(), ev)
        assert is_confident_non_sales(decision), (ev.external_id, decision)
        hs, _, report = _run(tmp_path, ev)
        assert hs.deals == []
        assert not any(w[0] == "upsert_deal" for w in hs.writes)


def _run(tmp_path, ev):
    settings = make_settings()
    memory = Memory(settings, data_dir=tmp_path / ev.external_id)
    report = CycleReport()
    hs = FakeHubSpot()
    _handle_engagement(ev, settings, hs, memory, None, report)
    return hs, memory, report


def test_staleness_two_business_days():
    friday = datetime(2026, 9, 18, 14, 0, tzinfo=timezone.utc)
    wednesday = datetime(2026, 9, 30, 18, 0, tzinfo=timezone.utc)
    assert business_days_between(friday, wednesday) > 2
    assert is_stale(friday, wednesday)
    assert not is_stale(datetime(2026, 9, 29, 12, tzinfo=timezone.utc), wednesday)


def test_dry_run_prints_diff_without_writing(tmp_path):
    ev = Engagement(
        source="calendly",
        external_id="dry-1",
        email="lklein@grnplano.com",
        first_name="Laura",
        last_name="Klein",
        raw_subject="SalesGlider Intro",
        extra={"event_type": "SalesGlider Intro"},
    )
    hs = FakeHubSpot()
    report = CycleReport(dry_run=True)
    apply_timeline(
        build_timelines([ev])["email:lklein@grnplano.com"],
        make_settings(dry_run=True),
        hs,
        Memory(make_settings(), data_dir=tmp_path),
        report,
        dry_run=True,
    )
    assert hs.contacts == []
    assert hs.deals == []
    blob = " ".join(str(x) for x in report.proposed_writes)
    assert "Laura" in blob or "lklein" in blob
