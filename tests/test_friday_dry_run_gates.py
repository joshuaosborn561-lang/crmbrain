"""Friday dry-run review: person-level intent, hire/contractor, Josh addresses, gates, 429."""

from datetime import datetime, timezone

import requests

from crmbrain.config import STAGE, is_josh_address
from crmbrain.cycle import (
    _handle_engagement,
    apply_gmail_stage_update,
    cycle_status,
    process_exit_code,
    run as cycle_run,
)
from crmbrain.documents import looks_josh_pays_document, stage_from_signature_mail
from crmbrain.hubspot import HubSpot, MAX_READ_RETRIES
from crmbrain.intent import INTENT_PROMPT, attach_person_intent, heuristic_intent
from crmbrain.memory import Memory
from crmbrain.models import CycleReport, Engagement
from crmbrain.policy import CONFIDENT_NO_INTENTS
from crmbrain.sources import gmail_scan
from tests.test_crm_gating import FakeHubSpot, make_settings


GABRIEL_EMAIL = "gabriel.lopez@rocketbox.mx"


def _gabriel_calendly() -> Engagement:
    return Engagement(
        source="gmail",
        external_id="cal-gabriel",
        email=GABRIEL_EMAIL,
        first_name="Gabriel",
        last_name="Lopez",
        name="Gabriel Lopez",
        raw_subject="New Event: Gabriel Lopez - SalesGlider Intro",
        summary="SalesGlider Intro with Gabriel Lopez",
        stage_hint=STAGE["discovery_scheduled"],
        extra={
            "create_new": True,
            "event_type": "SalesGlider Intro",
            "skip_lookback": True,
        },
        occurred_at=datetime(2026, 10, 2, 15, 0, tzinfo=timezone.utc),
    )


def _gabriel_fireflies() -> Engagement:
    return Engagement(
        source="fireflies",
        external_id="ff-gabriel",
        email=GABRIEL_EMAIL,
        first_name="Gabriel",
        last_name="Lopez",
        name="Gabriel Lopez",
        raw_subject="Gabriel Lopez and Joshua Osborn",
        transcript=(
            "Josh is hiring you, Gabriel, as a caller. We'd pay you for a contractor "
            "agreement / your trial with SalesGlider as a caller. Rocketbox. Not a client."
        ),
        extra={"skip_lookback": True},
        occurred_at=datetime(2026, 10, 2, 16, 0, tzinfo=timezone.utc),
    )


def test_hire_intent_in_heuristic_and_gemini_prompt():
    assert "hire" in CONFIDENT_NO_INTENTS
    assert "contractor" in CONFIDENT_NO_INTENTS
    assert "hire" in INTENT_PROMPT
    assert "contractor" in INTENT_PROMPT
    ev = Engagement(
        source="fireflies",
        external_id="hire-1",
        email="sdr@example.com",
        first_name="Ada",
        last_name="Caller",
        transcript="Josh is hiring you for a contractor agreement. We'd pay you as a caller.",
        raw_subject="Ada Caller and Joshua Osborn",
    )
    decision = heuristic_intent(ev)
    assert decision.verdict == "no"
    assert decision.intent == "hire"


def test_gabriel_lopez_calendly_plus_fireflies_hire_creates_no_deal(tmp_path):
    cal = _gabriel_calendly()
    ff = _gabriel_fireflies()
    settings = make_settings()
    attach_person_intent(settings, [cal, ff])
    assert getattr(cal, "_person_intent").intent == "hire"
    hs = FakeHubSpot()
    report = CycleReport()
    memory = Memory(settings, data_dir=tmp_path)
    apply_gmail_stage_update(
        cal,
        settings,
        hs,
        memory,
        None,
        report,
        held_events=[ff],
    )
    assert hs.deals == []
    assert hs.contacts == []
    assert not any(w[0] == "upsert_deal" for w in hs.writes)
    assert not any(w[0] == "upsert_contact" for w in hs.writes)
    assert any("excluded" in s or "hire" in s for s in report.skipped) or report.review_queue


def test_gabriel_lopez_cycle_creates_no_deal(tmp_path, monkeypatch):
    settings = make_settings(hubspot_token="tok", gmail_refresh_token="r")
    hs = FakeHubSpot()
    memory = Memory(settings, data_dir=tmp_path)
    cal = _gabriel_calendly()
    ff = _gabriel_fireflies()

    class Snap:
        upcoming = set()
        recent = set()
        create_engagements = []
        events = []
        calendar_api_ok = True
        calendar_api_error = ""

        def protect_emails(self):
            return set()

    monkeypatch.setattr("crmbrain.cycle.HubSpot", lambda settings: hs)
    monkeypatch.setattr("crmbrain.cycle.Memory", lambda settings: memory)
    monkeypatch.setattr("crmbrain.cycle.Gmail", lambda settings: object())
    monkeypatch.setattr("crmbrain.cycle.calendar_events.load_calendar", lambda *a, **k: Snap())
    monkeypatch.setattr("crmbrain.cycle.has_drive_access", lambda *a, **k: True)
    monkeypatch.setattr("crmbrain.cycle.cube_acr.scan", lambda *a, **k: [])
    monkeypatch.setattr("crmbrain.cycle.fireflies.scan", lambda *a, **k: [ff])
    monkeypatch.setattr("crmbrain.cycle.smartlead.scan", lambda *a, **k: [])
    monkeypatch.setattr("crmbrain.cycle.rvm.scan", lambda *a, **k: [])
    monkeypatch.setattr("crmbrain.cycle.gmail_scan.scan_people", lambda *a, **k: [])
    monkeypatch.setattr("crmbrain.cycle.gmail_scan.scan", lambda *a, **k: [cal])
    monkeypatch.setattr("crmbrain.cycle.enrichment.enrich", lambda *a, ev, **k: ev)
    monkeypatch.setattr("crmbrain.cycle.briefing.send_due", lambda *a, **k: None)

    report = cycle_run(settings)
    assert hs.deals == []
    assert hs.contacts == []
    assert not any(w[0] == "upsert_deal" for w in hs.writes)
    assert any("excluded" in s or "hire" in s for s in report.skipped) or report.review_queue


def test_contractor_agreement_pandadoc_is_not_signed():
    subject = "Document completed"
    sender = "PandaDoc <noreply@pandadoc.com>"
    body = "Contractor Agreement: 30-Day Trial with SalesGlider has been completed."
    assert looks_josh_pays_document(subject, body, "Contractor Agreement: 30-Day Trial with SalesGlider")
    stage, _amount, name = stage_from_signature_mail(
        subject,
        sender,
        "has been completed",
        body,
    )
    assert stage == ""
    assert "contractor" in name.lower() or "trial" in body.lower()


def test_contractor_agreement_apply_gmail_creates_no_deal(tmp_path):
    settings = make_settings()
    ev = Engagement(
        source="gmail",
        external_id="pd-contractor",
        email="ada@salesglider-trial.com",
        first_name="Ada",
        last_name="Caller",
        raw_subject="Contractor Agreement: 30-Day Trial with SalesGlider has been completed",
        summary="has been completed",
        stage_hint="",
        extra={
            "document_name": "Contractor Agreement: 30-Day Trial with SalesGlider",
            "document_id": "pd-trial-1",
        },
    )
    contact = {
        "id": "c-ada",
        "properties": {
            "email": "ada@salesglider-trial.com",
            "firstname": "Ada",
            "lastname": "Caller",
        },
    }
    hs = FakeHubSpot([contact])
    report = CycleReport()
    apply_gmail_stage_update(ev, settings, hs, Memory(settings, data_dir=tmp_path), None, report)
    assert not any(w[0] == "upsert_deal" for w in hs.writes)
    assert hs.deals == []


def test_is_josh_address_covers_insight_and_domains():
    assert is_josh_address("joshua.osborn@insight.com")
    assert is_josh_address("coworker@insight.com")
    assert is_josh_address("joshua@salesglidergrowth.com")
    assert not is_josh_address("gabriel.lopez@rocketbox.mx")


def test_scan_people_drops_insight_josh_address():
    class FakeGmail:
        def search(self, query, max_results=40):
            return [{"id": "m-insight"}]

        def get(self, mid):
            return {"internalDate": "1728000000000", "snippet": "Meraki Discussion"}

        def headers_map(self, _msg):
            return {
                "from": "Joshua Osborn <joshua.osborn@insight.com>",
                "to": "Joshua <joshua@salesglidergrowth.com>",
                "subject": "Meraki Discussion",
            }

    out = gmail_scan.scan_people(make_settings(), FakeGmail())
    assert out == []


def test_handle_drops_insight_josh_no_notes_or_updates(tmp_path):
    ev = Engagement(
        source="fireflies",
        external_id="ff-josh-insight",
        email="joshua.osborn@insight.com",
        first_name="Joshua",
        last_name="Osborn",
        transcript="SalesGlider discovery call about pricing and the monthly retainer.",
        raw_subject="Joshua Osborn Insight",
    )
    hs = FakeHubSpot()
    report = CycleReport()
    _handle_engagement(ev, make_settings(), hs, Memory(make_settings(), data_dir=tmp_path), None, report)
    assert hs.writes == []
    assert hs.notes == []
    assert hs.patches == []
    assert hs.contacts == []
    assert any("josh address" in s for s in report.skipped)


def test_dry_run_gmail_person_and_smartlead_are_skip_not_create(tmp_path):
    settings = make_settings(dry_run=True)
    memory = Memory(settings, data_dir=tmp_path)
    hs = FakeHubSpot()
    report = CycleReport(dry_run=True)
    person = Engagement(
        source="gmail_person",
        external_id="gm-cold",
        email="cold@example.com",
        first_name="Cold",
        last_name="Lead",
    )
    sl = Engagement(
        source="smartlead",
        external_id="sl-cold",
        email="pat@acme.com",
        first_name="Pat",
        last_name="Lee",
        summary="Positive SmartLead reply (Interested)",
    )
    _handle_engagement(person, settings, hs, memory, None, report)
    _handle_engagement(sl, settings, hs, memory, None, report)
    actions = [p.get("action") for p in report.proposed_writes]
    assert any("no meeting, skip HubSpot" in s for s in report.skipped)
    assert "create" in actions
    assert any(p.get("stage") == STAGE["initial_interest"] for p in report.proposed_writes)
    assert hs.writes == []
    assert hs.contacts == []
    assert hs.deals == []


def test_hubspot_search_retries_429_with_jitter(monkeypatch):
    hs = HubSpot(make_settings())
    slept = []
    monkeypatch.setattr("crmbrain.hubspot._sleep", slept.append)
    monkeypatch.setattr("crmbrain.hubspot.random.random", lambda: 0.5)
    calls = {"n": 0}

    class RateLimited:
        status_code = 429
        headers = {"Retry-After": "2"}
        text = "Too Many Requests"

        def json(self):
            return {"results": []}

        def raise_for_status(self):
            raise requests.HTTPError("429 Client Error: Too Many Requests")

    class Ok:
        status_code = 200
        headers = {}
        text = "{}"

        def json(self):
            return {"results": []}

        def raise_for_status(self):
            return None

    def fake_request(method, url, timeout=None, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return RateLimited()
        return Ok()

    hs.session.request = fake_request
    assert hs.find_contact(email="a@b.com") is None
    assert calls["n"] == 2
    assert slept and slept[0] > 0


def test_hubspot_search_429_exhausted_still_raises(monkeypatch):
    hs = HubSpot(make_settings())
    monkeypatch.setattr("crmbrain.hubspot._sleep", lambda _s: None)
    calls = {"n": 0}

    class RateLimited:
        status_code = 429
        headers = {}
        text = "Too Many Requests"

        def json(self):
            return {"results": []}

        def raise_for_status(self):
            raise requests.HTTPError("429 Client Error: Too Many Requests")

    def always_429(method, url, timeout=None, **kwargs):
        calls["n"] += 1
        return RateLimited()

    hs.session.request = always_429
    try:
        hs.find_contact(email="a@b.com")
        raise AssertionError("expected HTTPError")
    except requests.HTTPError:
        pass
    assert calls["n"] == MAX_READ_RETRIES + 1


def test_staffing_hiring_nurses_stays_on_sales_path(tmp_path):
    ev = Engagement(
        source="calendly",
        external_id="cal-staff",
        email="nina@staffingpro.com",
        first_name="Nina",
        last_name="Reyes",
        company="Staffing Pro",
        raw_subject="New Event: Nina Reyes - SalesGlider Intro",
        summary="We're hiring 20 nurses this quarter and need owner meetings.",
        extra={"event_type": "SalesGlider Intro"},
    )
    decision = heuristic_intent(ev)
    assert decision.intent != "hire"
    assert decision.verdict == "yes"
    hs, _, report = _handle_via(tmp_path, ev)
    assert hs.deals
    assert hs.deals[0]["properties"]["dealstage"] == STAGE["discovery_scheduled"]
    assert not any("hire" in s for s in report.skipped)


def test_msp_cold_caller_not_working_stays_on_sales_path(tmp_path):
    ev = Engagement(
        source="fireflies",
        external_id="ff-msp",
        email="rob@cyberguard360.com",
        first_name="Robert",
        last_name="Lawson",
        company="CyberGuard360",
        raw_subject="Robert Lawson and Joshua Osborn",
        transcript=(
            "This is a discovery call. Our cold caller isn't working. "
            "We want SalesGlider to book meetings with MSP owners."
        ),
    )
    decision = heuristic_intent(ev)
    assert decision.intent != "hire"
    assert decision.verdict == "yes"
    hs, _, report = _handle_via(tmp_path, ev)
    assert hs.deals
    assert hs.deals[0]["properties"]["dealstage"] == STAGE["discovery_completed"]
    assert not any("hire" in s for s in report.skipped)


def _handle_via(tmp_path, ev, hs=None):
    settings = make_settings()
    memory = Memory(settings, data_dir=tmp_path)
    report = CycleReport()
    hs = hs or FakeHubSpot()
    _handle_engagement(ev, settings, hs, memory, None, report)
    return hs, memory, report


def test_client_paid_trial_pandadoc_reaches_signed():
    assert not looks_josh_pays_document(
        "Document completed",
        "Paid Trial / Pilot with SalesGlider has been signed. Investment $3000.",
        "Paid Trial with SalesGlider",
    )
    stage, amount, _name = stage_from_signature_mail(
        "Document completed",
        "PandaDoc <noreply@pandadoc.com>",
        "has been completed",
        "Paid Trial / Pilot with SalesGlider has been signed. Investment $3000.",
    )
    assert stage == STAGE["signed"]
    assert amount == "3000"


def test_hire_no_does_not_override_client_document(tmp_path):
    settings = make_settings()
    ff = Engagement(
        source="fireflies",
        external_id="ff-hire-doc",
        email="pat@clientco.com",
        first_name="Pat",
        last_name="Lee",
        transcript="Josh is hiring you? Wait no — we also walked the Growth Partners agreement.",
        raw_subject="Pat Lee and Joshua Osborn",
    )
    doc = Engagement(
        source="gmail",
        external_id="pd-client",
        email="pat@clientco.com",
        first_name="Pat",
        last_name="Lee",
        raw_subject="Document completed: Growth Partners Agreement",
        summary="has been completed. Investment $21000.",
        stage_hint=STAGE["signed"],
        extra={"document_name": "Growth Partners Agreement", "document_id": "pd-gp-1"},
    )
    attach_person_intent(settings, [ff, doc])
    contact = {
        "id": "c-pat",
        "properties": {"email": "pat@clientco.com", "firstname": "Pat", "lastname": "Lee"},
    }
    hs = FakeHubSpot([contact])
    memory = Memory(settings, data_dir=tmp_path)
    report = CycleReport()
    apply_gmail_stage_update(doc, settings, hs, memory, None, report, held_events=[ff])
    assert any(w[0] == "upsert_deal" and w[2] == STAGE["signed"] for w in hs.writes)
    assert report.review_queue
    assert not memory.already_processed("gmail", "pd-client")


def test_gmail_people_reads_both_queries_prioritizes_crm_and_carries_overflow(tmp_path):
    class FakeGmail:
        def search(self, query, max_results=80):
            prefix = "s" if "in:sent" in query else "i"
            return [{"id": f"{prefix}{n}"} for n in range(50)]

        def get(self, mid):
            n = int(mid[1:])
            email = "crm@known.com" if mid == "i0" else f"{mid}@example.com"
            return {
                "id": mid,
                "internalDate": str(1_728_000_000_000 + n),
                "snippet": "hello",
                "_headers": {
                    "from": f"Person <{email}>",
                    "to": "Joshua <joshua@salesglidergrowth.com>",
                    "subject": f"thread {mid}",
                },
            }

        def headers_map(self, msg):
            return msg["_headers"]

    gmail = FakeGmail()
    settings = make_settings()
    memory = Memory(settings, data_dir=tmp_path)
    hs = FakeHubSpot(
        [{"id": "c-known", "properties": {"email": "crm@known.com", "firstname": "Known"}}]
    )
    hs.deals.append({"id": "d1", "contact_id": "c-known", "properties": {"dealstage": STAGE["discovery_scheduled"]}})
    report = CycleReport()
    first = gmail_scan.scan_people(settings, gmail, hubspot=hs, memory=memory, report=report)
    emails = {ev.email for ev in first}
    assert "crm@known.com" in emails
    assert len(first) == gmail_scan.MAX_GMAIL_PEOPLE
    assert report.gmail_people_overflow == 20
    assert memory.get_gmail_people_overflow()
    overflow_emails = {row["email"] for row in memory.get_gmail_people_overflow()}
    assert overflow_emails.isdisjoint(emails)

    class EmptyGmail:
        def search(self, query, max_results=80):
            return []

        def get(self, mid):
            raise AssertionError("should replay overflow, not refetch")

        def headers_map(self, msg):
            return {}

    report2 = CycleReport()
    replayed = gmail_scan.scan_people(settings, EmptyGmail(), hubspot=hs, memory=memory, report=report2)
    assert replayed
    assert all((ev.extra or {}).get("skip_lookback") for ev in replayed)
    assert report2.gmail_people_overflow == 0


def test_process_exit_code_dry_run_and_hubspot_429():
    dry = CycleReport(dry_run=True, errors=["gmail: boom"])
    assert process_exit_code(dry) == 0
    hs429 = CycleReport()
    hs429.errors.append("hubspot POST /crm/v3/objects/contacts/search: 429 Too Many Requests api.hubapi.com")
    assert cycle_status(hs429) == "ok"
    assert process_exit_code(hs429) == 0
    sl = CycleReport()
    sl.errors.append("smartlead campaign 3739758: 429 Client Error: Too Many Requests")
    assert cycle_status(sl) == "partial"
    assert process_exit_code(sl) == 1

