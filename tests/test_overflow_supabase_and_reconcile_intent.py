"""Gmail overflow in Supabase, and reconcile person-level confident-no gates."""

from datetime import datetime, timezone

from crmbrain.config import STAGE
from crmbrain.evidence import build_timelines
from crmbrain.intent import attach_person_intent, heuristic_intent
from crmbrain.memory import Memory
from crmbrain.models import CycleReport, Engagement
from crmbrain.reconcile import apply_timeline, restore_missing_deals
from tests.test_crm_gating import FakeHubSpot, make_settings


def _hire_fireflies(email="gabriel.lopez@rocketbox.mx", name="Gabriel Lopez") -> Engagement:
    first, last = name.split(" ", 1)
    return Engagement(
        source="fireflies",
        external_id="ff-hire",
        email=email,
        first_name=first,
        last_name=last,
        name=name,
        raw_subject=f"{name} and Joshua Osborn",
        transcript="Josh is hiring you as a caller. We'd pay you under a contractor agreement.",
        extra={"skip_lookback": True},
        occurred_at=datetime(2026, 10, 2, 16, 0, tzinfo=timezone.utc),
    )


def _calendly_intro(email="gabriel.lopez@rocketbox.mx", name="Gabriel Lopez") -> Engagement:
    first, last = name.split(" ", 1)
    return Engagement(
        source="calendly",
        external_id="gcal-hire",
        email=email,
        first_name=first,
        last_name=last,
        name=name,
        raw_subject=f"SalesGlider Intro - {name}",
        extra={"gcal_create": True, "create_new": True, "skip_lookback": True, "event_type": "SalesGlider Intro"},
        occurred_at=datetime(2026, 10, 2, 17, 0, tzinfo=timezone.utc),
    )


def test_gmail_overflow_writes_supabase_and_dry_run_does_not(tmp_path):
    settings = make_settings(supabase_url="https://example.supabase.co", supabase_key="secret")
    memory = Memory(settings, data_dir=tmp_path)
    store: list[dict] = []
    calls: list[str] = []

    def fake_sb(method, table, json_body=None, params=None):
        nonlocal store
        assert table == "gmail_people_overflow"
        calls.append(method)
        if method == "GET":
            return list(store)
        if method == "DELETE":
            store = []
            return []
        if method == "POST":
            store = list(json_body) if isinstance(json_body, list) else [json_body]
            return store
        raise AssertionError(method)

    memory._sb_schema = fake_sb
    row = {
        "email": "overflow@example.com",
        "external_id": "m-over",
        "first_name": "Over",
        "last_name": "Flow",
        "name": "Over Flow",
        "domain": "example.com",
        "company": "Example",
        "raw_subject": "hello",
        "summary": "hi",
        "occurred_at": "2026-10-02T12:00:00+00:00",
        "extra": {"skip_lookback": True},
    }
    memory.set_gmail_people_overflow([row])
    assert "DELETE" in calls and "POST" in calls
    later = Memory(settings, data_dir=tmp_path / "other")
    later._sb_schema = fake_sb
    got = later.get_gmail_people_overflow()
    assert got[0]["email"] == "overflow@example.com"
    assert got[0]["external_id"] == "m-over"
    assert later._local.get("gmail_people_overflow")[0]["email"] == "overflow@example.com"

    dry = make_settings(supabase_url="https://example.supabase.co", supabase_key="secret", dry_run=True)
    dry_mem = Memory(dry, data_dir=tmp_path / "dry")
    dry_calls: list[str] = []
    dry_mem._sb_schema = lambda *a, **k: dry_calls.append(a[0])
    dry_mem.set_gmail_people_overflow([row])
    assert dry_calls == []
    assert dry_mem._local["gmail_people_overflow"][0]["email"] == "overflow@example.com"

    consume = Memory(dry, data_dir=tmp_path / "consume")
    consume_calls: list[str] = []

    def consume_sb(method, table, json_body=None, params=None):
        consume_calls.append(method)
        assert table == "gmail_people_overflow"
        if method == "GET":
            return [row]
        raise AssertionError(method)

    consume._sb_schema = consume_sb
    carried = consume.get_gmail_people_overflow()
    assert consume_calls == ["GET"]
    assert carried[0]["email"] == "overflow@example.com"
    assert carried[0]["extra"]["skip_lookback"] is True


def test_apply_timeline_and_restore_skip_hire_calendar_create(tmp_path):
    cal = _calendly_intro()
    ff = _hire_fireflies()
    settings = make_settings()
    attach_person_intent(settings, [cal, ff])
    assert heuristic_intent(ff).intent == "hire"
    timelines = build_timelines([cal, ff])
    hs = FakeHubSpot()
    report = CycleReport()
    memory = Memory(settings, data_dir=tmp_path)
    apply_timeline(timelines["email:gabriel.lopez@rocketbox.mx"], settings, hs, memory, report)
    restore_missing_deals(hs, settings, memory, report, timelines)
    assert hs.deals == []
    assert hs.contacts == []
    assert report.review_queue
    assert any("hire" in s for s in report.skipped) or any("hire" in x for x in report.review_queue)


def test_restore_missing_deals_skips_day_job_calendar_create(tmp_path):
    ev = Engagement(
        source="calendly",
        external_id="gcal-insight",
        email="pat@insight.com",
        first_name="Pat",
        last_name="Lee",
        raw_subject="Meraki Discussion",
        extra={"gcal_create": True, "create_new": True, "skip_lookback": True},
    )
    settings = make_settings()
    timelines = build_timelines([ev])
    hs = FakeHubSpot()
    report = CycleReport()
    restore_missing_deals(
        hs, settings, Memory(settings, data_dir=tmp_path), report, timelines
    )
    assert hs.deals == []
    assert hs.contacts == []


def test_apply_timeline_hire_does_not_block_client_signed_doc(tmp_path):
    ff = _hire_fireflies(email="pat@clientco.com", name="Pat Lee")
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
        occurred_at=datetime(2026, 10, 2, 18, 0, tzinfo=timezone.utc),
    )
    settings = make_settings()
    timelines = build_timelines([ff, doc])
    key = "email:pat@clientco.com"
    contact = {
        "id": "c-pat",
        "properties": {"email": "pat@clientco.com", "firstname": "Pat", "lastname": "Lee"},
    }
    hs = FakeHubSpot([contact])
    timelines[key].contact = contact
    report = CycleReport()
    apply_timeline(timelines[key], settings, hs, Memory(settings, data_dir=tmp_path), report)
    assert any(w[0] == "upsert_deal" and w[2] == STAGE["signed"] for w in hs.writes)
    assert hs.deals
    assert hs.deals[0]["properties"]["dealstage"] == STAGE["signed"]
    assert report.review_queue


def test_apply_timeline_skips_vendor_and_personal_calendar_create(tmp_path):
    vendor = Engagement(
        source="calendly",
        external_id="gcal-seth",
        email="seth@partner.com",
        first_name="Seth",
        last_name="Kingdon",
        name="Seth Kingdon",
        raw_subject="SEO partner sync",
        extra={"gcal_create": True, "create_new": True, "skip_lookback": True},
    )
    personal = Engagement(
        source="calendly",
        external_id="gcal-alex",
        email="alex@example.com",
        first_name="Alex",
        last_name="Branning",
        name="Alex Branning",
        raw_subject="Lunch catch up",
        extra={"gcal_create": True, "create_new": True, "skip_lookback": True},
    )
    settings = make_settings()
    for ev in (vendor, personal):
        timelines = build_timelines([ev])
        hs = FakeHubSpot()
        report = CycleReport()
        memory = Memory(settings, data_dir=tmp_path / ev.external_id)
        apply_timeline(next(iter(timelines.values())), settings, hs, memory, report)
        restore_missing_deals(hs, settings, memory, report, timelines)
        assert hs.deals == []
        assert hs.contacts == []
