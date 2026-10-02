"""Friday dry-run review: person-level intent, hire/contractor, Josh addresses, gates, 429."""

from datetime import datetime, timezone

import requests

from crmbrain.config import STAGE, is_josh_address
from crmbrain.cycle import _handle_engagement, apply_gmail_stage_update, run as cycle_run
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
            "Josh is hiring Gabriel Lopez as a cold caller for a 30-day paid trial "
            "SDR contractor role. Rocketbox. This is a recruiting interview, not a client."
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
        transcript="Josh is hiring a cold caller for a 30-day paid trial SDR seat.",
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
    apply_gmail_stage_update(
        cal,
        settings,
        hs,
        Memory(settings, data_dir=tmp_path),
        None,
        report,
        held_events=[ff],
    )
    assert hs.deals == []
    assert hs.contacts == []
    assert not any(w[0] == "upsert_deal" for w in hs.writes)
    assert not any(w[0] == "upsert_contact" for w in hs.writes)
    assert report.review_queue
    assert any("hire" in s for s in report.skipped)


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
    assert any("hire" in (s + " ".join(report.review_queue)) for s in report.skipped) or report.review_queue


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
    assert "create" not in actions
    assert any("no meeting, skip HubSpot" in s for s in report.skipped)
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
