"""Gmail overflow in Supabase, and reconcile person-level confident-no gates."""

from datetime import datetime, timezone

from crmbrain.config import STAGE
from crmbrain.evidence import build_timelines
from crmbrain.intent import attach_person_intent, heuristic_intent
from crmbrain.memory import Memory
from crmbrain.models import CycleReport, Engagement
from crmbrain.reconcile import apply_timeline, restore_missing_deals
from crmbrain.sources import gmail_scan
from tests.test_crm_gating import FakeHubSpot, make_settings


def _overflow_row(email: str, external_id: str) -> dict:
    return {
        "email": email,
        "external_id": external_id,
        "first_name": email.split("@")[0].title(),
        "last_name": "Overflow",
        "name": f"{email.split('@')[0].title()} Overflow",
        "domain": email.split("@")[1],
        "company": "",
        "raw_subject": "hello",
        "summary": "hi",
        "occurred_at": "2026-10-02T12:00:00+00:00",
        "extra": {"skip_lookback": True},
    }


def _overflow_store(initial: list[dict] | None = None):
    store = list(initial or [])
    calls: list[tuple] = []

    def fake_sb(method, table, json_body=None, params=None):
        nonlocal store
        assert table == "gmail_people_overflow"
        calls.append((method, json_body, params))
        if method == "GET":
            return list(store)
        if method == "POST":
            incoming = json_body if isinstance(json_body, list) else [json_body]
            by_email = {row["email"]: row for row in store}
            for row in incoming:
                by_email[row["email"]] = row
            store[:] = list(by_email.values())
            return incoming
        if method == "DELETE":
            email = str((params or {}).get("email") or "")
            if not email.startswith("eq."):
                raise AssertionError(f"must not wipe overflow: {params}")
            key = email[3:]
            store[:] = [row for row in store if row["email"] != key]
            return []
        raise AssertionError(method)

    return store, calls, fake_sb


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
    store, calls, fake_sb = _overflow_store()
    memory._sb_schema = fake_sb
    row = _overflow_row("overflow@example.com", "m-over")
    memory.set_gmail_people_overflow([row])
    assert any(method == "POST" for method, _, _ in calls)
    assert not any(method == "DELETE" for method, _, _ in calls)
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


def test_overflow_survives_crash_before_handle_and_failed_insert(tmp_path):
    settings = make_settings(supabase_url="https://example.supabase.co", supabase_key="secret")
    alice = _overflow_row("alice@ex.com", "m-alice")
    bob = _overflow_row("bob@ex.com", "m-bob")
    store, calls, fake_sb = _overflow_store([alice, bob])
    memory = Memory(settings, data_dir=tmp_path)
    memory._sb_schema = fake_sb

    class EmptyGmail:
        def search(self, query, max_results=80):
            return []

        def get(self, mid):
            raise AssertionError("should replay overflow")

        def headers_map(self, msg):
            return {}

    selected = gmail_scan.scan_people(
        settings, EmptyGmail(), memory=memory, report=CycleReport()
    )
    assert {ev.email for ev in selected} == {"alice@ex.com", "bob@ex.com"}
    assert {row["email"] for row in store} == {"alice@ex.com", "bob@ex.com"}
    assert not any(method == "DELETE" for method, _, _ in calls)

    memory.drop_gmail_people_overflow("alice@ex.com")
    assert {row["email"] for row in store} == {"bob@ex.com"}
    assert any(method == "DELETE" and (params or {}).get("email") == "eq.alice@ex.com" for method, _, params in calls)

    def boom(method, table, json_body=None, params=None):
        if method == "POST":
            raise RuntimeError("insert failed")
        return fake_sb(method, table, json_body, params)

    memory._sb_schema = boom
    memory.errors.clear()
    memory.upsert_gmail_people_overflow([_overflow_row("carol@ex.com", "m-carol")])
    assert memory.errors
    assert {row["email"] for row in store} == {"bob@ex.com"}


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


def _completed_pandadoc(email: str, name: str, title: str, external_id: str) -> Engagement:
    first, last = name.split(" ", 1)
    return Engagement(
        source="gmail",
        external_id=external_id,
        email=email,
        first_name=first,
        last_name=last,
        name=name,
        raw_subject=f"Document completed: {title}",
        summary=f"{title} has been completed.",
        stage_hint=STAGE["signed"],
        extra={"document_name": title, "document_id": external_id},
        occurred_at=datetime(2026, 10, 2, 18, 0, tzinfo=timezone.utc),
    )


def test_mentor_and_vendor_completed_pandadoc_get_no_deal(tmp_path):
    mentor_call = Engagement(
        source="fireflies",
        external_id="ff-mentor",
        email="mark@mentor.com",
        first_name="Mark",
        last_name="Mentor",
        name="Mark Mentor",
        raw_subject="Mark/Josh recurring 1:1",
        transcript="Mentor session. Recurring Mark/Josh 1:1, not a sales call.",
        extra={"skip_lookback": True},
        occurred_at=datetime(2026, 10, 2, 16, 0, tzinfo=timezone.utc),
    )
    mentor_doc = _completed_pandadoc(
        "mark@mentor.com", "Mark Mentor", "Mentor Program Agreement", "pd-mentor"
    )
    vendor_call = Engagement(
        source="fireflies",
        external_id="ff-vendor",
        email="seth@partner.com",
        first_name="Seth",
        last_name="Kingdon",
        name="Seth Kingdon",
        raw_subject="SEO partner sync",
        transcript="Vendor / SEO partner sync with Seth Kingdon.",
        extra={"skip_lookback": True},
        occurred_at=datetime(2026, 10, 2, 16, 0, tzinfo=timezone.utc),
    )
    vendor_doc = _completed_pandadoc(
        "seth@partner.com", "Seth Kingdon", "SEO Partner Agreement", "pd-vendor"
    )
    settings = make_settings()
    assert heuristic_intent(mentor_call).intent == "mentor"
    assert heuristic_intent(vendor_call).intent == "vendor"
    for evs, key in (
        ([mentor_call, mentor_doc], "email:mark@mentor.com"),
        ([vendor_call, vendor_doc], "email:seth@partner.com"),
    ):
        timelines = build_timelines(evs)
        hs = FakeHubSpot()
        report = CycleReport()
        memory = Memory(settings, data_dir=tmp_path / key.replace(":", "-"))
        apply_timeline(timelines[key], settings, hs, memory, report)
        restore_missing_deals(hs, settings, memory, report, timelines)
        assert hs.deals == []
        assert hs.contacts == []
