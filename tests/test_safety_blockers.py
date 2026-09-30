"""PR #15 safety blockers: budgets, reeval, signed/paid, dry-run, freshness."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from crmbrain.budget import WriteBudget
from crmbrain.calendar_events import classify_gcal_event, may_create_contact_from_event, passes_calendar_create_gate
from crmbrain.config import STAGE
from crmbrain.cycle import _apply_transcript_intelligence, _stale_source_warning, cycle_status, run as cycle_run
from crmbrain.documents import looks_free_document
from crmbrain.evidence import build_timelines
from crmbrain.hubspot import _is_hubspot_mutation
from crmbrain.intent import heuristic_intent
from crmbrain.memory import Memory
from crmbrain.models import CycleReport, Engagement
from crmbrain.policy import (
    STAGE_RANK,
    choose_deal_action,
    has_closed_won_deal,
    has_poc_evidence,
    resolve_stage,
)
from crmbrain.reconcile import (
    _planned_change_count,
    apply_timeline,
    reeval_discovery_scheduled,
    run as reconcile_run,
)
from crmbrain.sources.allo import fetch_items_since
from crmbrain.sources.gmail_scan import mail_queries
from tests.test_crm_gating import FakeHubSpot, make_settings
from tests.test_qa_fixes import _gcal_event


def _past(hours=5):
    return datetime.now(timezone.utc) - timedelta(hours=hours)


def test_onboarding_extracted_signed_does_not_write_above_discovery_completed(tmp_path, monkeypatch):
    ev = Engagement(
        source="fireflies",
        external_id="ff-onboard",
        email="pat@acme.com",
        first_name="Pat",
        last_name="Lee",
        transcript="Onboarding starts Monday. Kickoff for the pilot next week.",
        raw_subject="Pat Lee onboarding",
    )
    stage = resolve_stage(ev, {"stage_hint": "signed"})
    assert stage == STAGE["discovery_completed"]
    assert STAGE_RANK[stage] <= STAGE_RANK[STAGE["discovery_completed"]]
    hs = FakeHubSpot(
        [{"id": "1", "properties": {"email": "pat@acme.com", "firstname": "Pat", "lastname": "Lee"}}]
    )
    hs.deals = [
        {
            "id": "d1",
            "contact_id": "1",
            "properties": {
                "dealstage": STAGE["discovery_completed"],
                "dealname": "Pat Lee",
            },
        }
    ]
    monkeypatch.setattr(
        "crmbrain.intelligence.extract",
        lambda settings, engagement: {"stage_hint": "signed"},
    )
    report = CycleReport()
    _apply_transcript_intelligence(
        ev,
        make_settings(),
        hs,
        Memory(make_settings(), data_dir=tmp_path),
        report,
        hs.contacts[0],
        add_timeline_note=False,
    )
    assert hs.deals[0]["properties"]["dealstage"] == STAGE["discovery_completed"]
    assert not any(w[0] == "upsert_deal" and w[2] == STAGE["signed"] for w in hs.writes)
    assert any("poc_hint" in x for x in report.review_queue)


def test_write_budget_thirty_discovery_scheduled_caps_at_ten(tmp_path):
    past = _past(5).isoformat()
    contacts = []
    deals = []
    engagements = []
    for i in range(30):
        contacts.append(
            {
                "id": str(i + 1),
                "properties": {"email": f"lead{i}@x.com", "firstname": "Lead", "lastname": str(i)},
            }
        )
        deals.append(
            {
                "id": f"d{i}",
                "contact_id": str(i + 1),
                "properties": {
                    "dealstage": STAGE["discovery_scheduled"],
                    "dealname": f"Lead {i}",
                    "meeting_at": past,
                },
            }
        )
        engagements.append(
            Engagement(
                source="calendly",
                external_id=f"cal-{i}",
                email=f"lead{i}@x.com",
                first_name="Lead",
                last_name=str(i),
                extra={"meeting_at": past},
                raw_subject="SalesGlider Intro",
            )
        )
    hs = FakeHubSpot(contacts)
    hs.deals = deals
    report = CycleReport()
    reeval_discovery_scheduled(
        hs,
        make_settings(),
        Memory(make_settings(), data_dir=tmp_path),
        report,
        build_timelines(engagements),
        upcoming_emails=set(),
        calendar_api_ok=True,
        budget=WriteBudget(max_archives_regressions=10, max_creates=10, max_stage_moves=20),
    )
    noshows = [d for d in hs.deals if (d.get("properties") or {}).get("dealstage") == STAGE["no_show"]]
    still = [d for d in hs.deals if (d.get("properties") or {}).get("dealstage") == STAGE["discovery_scheduled"]]
    assert len(noshows) == 10
    assert len(still) == 20
    assert sum(1 for x in report.review_queue if "cap" in x) == 20


def test_change_fraction_aborts_all_reconcile_writes(tmp_path):
    hs = FakeHubSpot(
        [{"id": "1", "properties": {"email": "a@x.com", "firstname": "A", "lastname": "One"}}]
    )
    hs.deals = [
        {
            "id": "d1",
            "contact_id": "1",
            "properties": {"dealstage": STAGE["discovery_scheduled"], "dealname": "A"},
        }
    ]
    ev = Engagement(
        source="calendly",
        external_id="c1",
        email="a@x.com",
        first_name="A",
        last_name="One",
        raw_subject="SalesGlider Intro",
        extra={"meeting_at": _past().isoformat()},
    )
    report = CycleReport()
    budget = WriteBudget(max_change_fraction=0.15)
    reconcile_run(
        make_settings(),
        hs,
        Memory(make_settings(), data_dir=tmp_path),
        report,
        [ev],
        upcoming_emails=set(),
        budget=budget,
        calendar_api_ok=True,
    )
    assert report.reconcile_aborted
    assert hs.deals[0]["properties"]["dealstage"] == STAGE["discovery_scheduled"]
    assert any("abort" in (w or "").lower() for w in report.warnings + report.skipped)


def test_reeval_skips_when_calendar_api_failed(tmp_path):
    hs = FakeHubSpot([{"id": "1", "properties": {"email": "a@x.com", "firstname": "A", "lastname": "One"}}])
    hs.deals = [
        {"id": "d1", "contact_id": "1", "properties": {"dealstage": STAGE["discovery_scheduled"], "dealname": "A"}}
    ]
    report = CycleReport()
    reeval_discovery_scheduled(
        hs,
        make_settings(),
        Memory(make_settings(), data_dir=tmp_path),
        report,
        {},
        upcoming_emails=set(),
        calendar_api_ok=False,
    )
    assert hs.deals[0]["properties"]["dealstage"] == STAGE["discovery_scheduled"]
    assert any("calendar api" in x for x in report.skipped)


def test_crm_source_never_counts_as_held(tmp_path):
    hs = FakeHubSpot(
        [
            {
                "id": "1",
                "properties": {
                    "email": "a@x.com",
                    "firstname": "A",
                    "lastname": "One",
                    "crm_source": "fireflies",
                },
            }
        ]
    )
    hs.deals = [
        {
            "id": "d1",
            "contact_id": "1",
            "properties": {
                "dealstage": STAGE["discovery_scheduled"],
                "dealname": "A",
                "meeting_at": _past().isoformat(),
            },
        }
    ]
    ev = Engagement(
        source="calendly",
        external_id="c1",
        email="a@x.com",
        extra={"meeting_at": _past().isoformat()},
        raw_subject="SalesGlider Intro",
    )
    report = CycleReport()
    reeval_discovery_scheduled(
        hs,
        make_settings(),
        Memory(make_settings(), data_dir=tmp_path),
        report,
        build_timelines([ev]),
        upcoming_emails=set(),
        held_events=[],
        calendar_api_ok=True,
    )
    assert hs.deals[0]["properties"]["dealstage"] == STAGE["no_show"]


def test_signed_not_moved_to_proposal_without_matching_document():
    ev = Engagement(
        source="gmail",
        external_id="pd",
        extra={"document_name": "Growth Partners Agreement", "amount": "21000"},
    )
    assert choose_deal_action(STAGE["signed"], STAGE["proposal_sent"], ev) is None
    deal = {"properties": {"document_name": "Growth Partners Agreement"}}
    assert choose_deal_action(STAGE["signed"], STAGE["proposal_sent"], ev, deal=deal) == STAGE["proposal_sent"]


def test_never_create_deal_when_contact_already_paid_or_signed(tmp_path):
    ev = Engagement(
        source="calendly",
        external_id="c1",
        email="paid@x.com",
        first_name="Paid",
        last_name="Client",
        raw_subject="SalesGlider Intro",
        extra={"event_type": "SalesGlider Intro"},
    )
    hs = FakeHubSpot(
        [{"id": "1", "properties": {"email": "paid@x.com", "firstname": "Paid", "lastname": "Client"}}]
    )
    hs.deals = [
        {"id": "paid-d", "contact_id": "1", "properties": {"dealstage": STAGE["paid"], "dealname": "Paid"}}
    ]
    timeline = build_timelines([ev])["email:paid@x.com"]
    timeline.contact = hs.contacts[0]
    timeline.deals = list(hs.deals)
    report = CycleReport()
    apply_timeline(timeline, make_settings(), hs, Memory(make_settings(), data_dir=tmp_path), report)
    assert len(hs.deals) == 1
    assert hs.deals[0]["properties"]["dealstage"] == STAGE["paid"]
    assert any("closed_won" in x for x in report.review_queue)
    assert has_closed_won_deal(hs.deals)


def test_paid_never_downgraded():
    ev = Engagement(source="gmail", external_id="x", stage_hint=STAGE["no_show"])
    assert choose_deal_action(STAGE["paid"], STAGE["no_show"], ev) is None
    assert choose_deal_action(STAGE["paid"], STAGE["discovery_scheduled"], ev) is None


def test_poc_hint_word_boundaries_and_no_gmail_query():
    ev = Engagement(
        source="fireflies",
        external_id="x",
        transcript="We talked about the apocalypse of lead gen and a epoch plan.",
        raw_subject="Chat",
    )
    assert not has_poc_evidence(ev)
    ev2 = Engagement(source="fireflies", external_id="y", raw_subject="Paid POC kickoff")
    assert has_poc_evidence(ev2)
    queries = " ".join(mail_queries(make_settings()))
    assert "kickoff" not in queries
    assert "onboarding" not in queries
    assert "proof of concept" not in queries


def test_cold_sources_are_no_before_sales_hints():
    for source in ("smartlead", "heyreach", "rvm", "gmail_person"):
        ev = Engagement(
            source=source,
            external_id=source,
            email="pat@acme.com",
            first_name="Pat",
            last_name="Lee",
            raw_subject="SalesGlider discovery proposal pricing",
            summary="Interested in a SalesGlider intro",
        )
        decision = heuristic_intent(ev)
        assert decision.verdict == "no", source


def test_twenty_one_thousand_agreement_is_not_free():
    assert not looks_free_document(
        "Dave viewed Growth Partners Agreement",
        "Investment $21,000.00 for the Growth Partners Agreement.",
        "Growth Partners Agreement",
    )
    assert looks_free_document("Completed", "Total $0.00 complimentary SOW", "Free SOW")
    assert looks_free_document("Completed", "Amount $0 no charge", "SOW")


def test_record_freshness_keeps_prior_last_item_at(tmp_path):
    settings = make_settings()
    mem = Memory(settings, data_dir=tmp_path)
    stamp = datetime(2026, 9, 18, 14, 0, tzinfo=timezone.utc)
    mem.record_freshness("gmail", last_item_at=stamp)
    mem.record_freshness("gmail", last_item_at=None)
    kept = mem.latest_freshness("gmail")
    assert kept == stamp


def test_calendar_401_and_missing_allo_are_warnings_not_errors():
    report = CycleReport()
    _stale_source_warning(
        report,
        "calendar",
        "calendar api 401 — grant Calendar readonly scope",
    )
    _stale_source_warning(report, "allo", "ALLO_API_KEY missing")
    assert cycle_status(report) == "ok"
    assert report.errors == []
    assert any(w.startswith("calendar:") for w in report.warnings)
    assert any(w.startswith("allo:") for w in report.warnings)
    errors: list[str] = []
    fetch_items_since(make_settings(allo_key=""), datetime.now(timezone.utc), errors=errors)
    assert errors == []


def test_hubspot_search_is_not_a_mutation():
    assert not _is_hubspot_mutation("POST", "/crm/v3/objects/deals/search")
    assert not _is_hubspot_mutation("GET", "/crm/v3/objects/contacts")
    assert _is_hubspot_mutation("POST", "/crm/v3/objects/deals")
    assert _is_hubspot_mutation("PATCH", "/crm/v3/objects/deals/1")
    assert _is_hubspot_mutation("DELETE", "/crm/v3/objects/deals/1")


def test_josh_one_on_one_without_sales_intent_does_not_create():
    event, now = _gcal_event(summary="Josh / Brian")
    classified = classify_gcal_event(event, now=now)
    assert passes_calendar_create_gate(classified)
    assert not may_create_contact_from_event(classified)
    sales, _ = _gcal_event(summary="SalesGlider Boyd Cold Email")
    assert may_create_contact_from_event(classify_gcal_event(sales, now=now))


def test_cycle_run_dry_run_zero_writes(tmp_path, monkeypatch):
    settings = make_settings(dry_run=True, hubspot_token="tok", gmail_refresh_token="r")
    hs = FakeHubSpot()
    memory = Memory(settings, data_dir=tmp_path)
    create = Engagement(
        source="calendly",
        external_id="dry-cycle",
        email="lklein@grnplano.com",
        first_name="Laura",
        last_name="Klein",
        raw_subject="SalesGlider Intro",
        extra={"event_type": "SalesGlider Intro", "gcal_create": True, "create_new": True},
    )

    class Snap:
        upcoming = set()
        recent = set()
        create_engagements = [create]
        events = []
        calendar_api_ok = True
        calendar_api_error = ""

        def protect_emails(self):
            return set()

    monkeypatch.setattr("crmbrain.cycle.HubSpot", lambda settings: hs)
    monkeypatch.setattr("crmbrain.cycle.Memory", lambda settings: memory)
    monkeypatch.setattr("crmbrain.cycle.Gmail", lambda settings: object())
    monkeypatch.setattr("crmbrain.cycle.calendar_events.load_calendar", lambda *a, **k: Snap())
    monkeypatch.setattr("crmbrain.cycle.cube_acr.scan", lambda *a, **k: [])
    monkeypatch.setattr("crmbrain.cycle.fireflies.scan", lambda *a, **k: [])
    monkeypatch.setattr("crmbrain.cycle.smartlead.scan", lambda *a, **k: [])
    monkeypatch.setattr("crmbrain.cycle.rvm.scan", lambda *a, **k: [])
    monkeypatch.setattr("crmbrain.cycle.allo.scan", lambda *a, **k: [])
    monkeypatch.setattr("crmbrain.cycle.gmail_scan.scan_people", lambda *a, **k: [])
    monkeypatch.setattr("crmbrain.cycle.gmail_scan.scan", lambda *a, **k: [])
    enrich = MagicMock(side_effect=AssertionError("enrichment called"))
    monkeypatch.setattr("crmbrain.cycle.enrichment.enrich", enrich)
    brief = MagicMock(side_effect=AssertionError("briefing called"))
    monkeypatch.setattr("crmbrain.cycle.briefing.send_due", brief)
    hey_add = MagicMock(side_effect=AssertionError("heyreach add_lead called"))
    monkeypatch.setattr("crmbrain.cycle.HeyReach.add_lead", hey_add, raising=False)

    report = cycle_run(settings)
    assert report.dry_run
    assert hs.writes == []
    assert hs.contacts == []
    assert hs.deals == []
    assert memory._local.get("processed") in (None, [])
    assert memory._local.get("ticker") in (None, [])
    assert memory._local.get("review_queue") in (None, [])
    assert memory._local.get("source_freshness") in (None, {})
    assert memory._local.get("facts") in (None, [])
    assert memory._local.get("allo_calls") in (None, [])
    assert memory._local.get("cube_acr_calls") in (None, [])
    assert all(w == "cycle_runs" for w in memory.writes)
    assert memory._local.get("runs")
    assert memory._local["runs"][-1]["status"] == "dry_run"
    assert memory.last_finished_run_started_at() is None
    assert report.proposed_writes
    enrich.assert_not_called()
    brief.assert_not_called()


def test_dry_run_status_is_ignored_by_lookback(tmp_path):
    settings = make_settings(dry_run=True)
    memory = Memory(settings, data_dir=tmp_path)
    memory._run_started_at = "2026-09-25T22:00:00+00:00"
    memory.dry_run = False
    memory.finish_run(None, "ok", {"ok": True})
    memory.dry_run = True
    memory._run_started_at = "2026-10-02T22:00:00+00:00"
    memory.finish_run(None, "dry_run", {"dry_run": True})
    stamp = memory.last_finished_run_started_at()
    assert stamp == datetime(2026, 9, 25, 22, 0, tzinfo=timezone.utc)
    statuses = [r.get("status") for r in memory._local["runs"]]
    assert "dry_run" in statuses
    assert statuses[-1] == "dry_run"


def test_planned_change_count_only_counts_reeval_that_would_write(tmp_path):
    past = _past(5).isoformat()
    contacts = []
    deals = []
    for i in range(10):
        contacts.append(
            {
                "id": str(i + 1),
                "properties": {"email": f"stay{i}@x.com", "firstname": "Stay", "lastname": str(i)},
            }
        )
        deals.append(
            {
                "id": f"stay{i}",
                "contact_id": str(i + 1),
                "properties": {"dealstage": STAGE["discovery_scheduled"], "dealname": f"Stay {i}"},
            }
        )
    contacts.append(
        {"id": "99", "properties": {"email": "move@x.com", "firstname": "Move", "lastname": "One"}}
    )
    deals.append(
        {
            "id": "move1",
            "contact_id": "99",
            "properties": {
                "dealstage": STAGE["discovery_scheduled"],
                "dealname": "Move",
                "meeting_at": past,
            },
        }
    )
    for i in range(20):
        contacts.append(
            {
                "id": f"dc{i}",
                "properties": {"email": f"dc{i}@x.com", "firstname": "Done", "lastname": str(i)},
            }
        )
        deals.append(
            {
                "id": f"dc{i}",
                "contact_id": f"dc{i}",
                "properties": {"dealstage": STAGE["discovery_completed"], "dealname": f"Done {i}"},
            }
        )
    hs = FakeHubSpot(contacts)
    hs.deals = deals
    n = _planned_change_count(
        hs,
        make_settings(),
        {},
        upcoming_emails=set(),
        held_events=[],
        calendar_api_ok=True,
    )
    assert n == 1


def test_dry_run_lists_writes_when_fraction_would_abort(tmp_path):
    past = _past(5).isoformat()
    contacts = []
    deals = []
    for i in range(30):
        contacts.append(
            {
                "id": str(i + 1),
                "properties": {"email": f"lead{i}@x.com", "firstname": "Lead", "lastname": str(i)},
            }
        )
        deals.append(
            {
                "id": f"d{i}",
                "contact_id": str(i + 1),
                "properties": {
                    "dealstage": STAGE["discovery_scheduled"],
                    "dealname": f"Lead {i}",
                    "meeting_at": past,
                },
            }
        )
    hs = FakeHubSpot(contacts)
    hs.deals = deals
    report = CycleReport(dry_run=True)
    reconcile_run(
        make_settings(dry_run=True),
        hs,
        Memory(make_settings(dry_run=True), data_dir=tmp_path),
        report,
        [],
        upcoming_emails=set(),
        dry_run=True,
        calendar_api_ok=True,
        budget=WriteBudget(max_archives_regressions=10, max_creates=10, max_stage_moves=20),
    )
    assert report.would_abort
    assert report.reconcile_aborted
    written = [w for w in report.proposed_writes if isinstance(w, dict) and w.get("action") == "move"]
    assert len(written) == 10
    assert sum(1 for x in report.review_queue if "cap" in x) == 20
    assert all((d.get("properties") or {}).get("dealstage") == STAGE["discovery_scheduled"] for d in hs.deals)


def test_nurture_cancel_blocked_by_calendar_or_future_meeting(tmp_path):
    hs = FakeHubSpot(
        [
            {"id": "1", "properties": {"email": "cal@x.com", "firstname": "Cal", "lastname": "Lead"}},
            {"id": "2", "properties": {"email": "fut@x.com", "firstname": "Fut", "lastname": "Lead"}},
        ]
    )
    hs.deals = [
        {"id": "d-cal", "contact_id": "1", "properties": {"dealstage": STAGE["discovery_scheduled"], "dealname": "Cal"}},
        {"id": "d-fut", "contact_id": "2", "properties": {"dealstage": STAGE["discovery_scheduled"], "dealname": "Fut"}},
    ]
    hs.recent_attendee_emails = {"cal@x.com"}
    hs.future_meetings = {"2"}
    canceled_cal = Engagement(
        source="calendly",
        external_id="cx-cal",
        email="cal@x.com",
        first_name="Cal",
        last_name="Lead",
        extra={"canceled": True},
        raw_subject="Canceled: SalesGlider Intro",
    )
    canceled_fut = Engagement(
        source="calendly",
        external_id="cx-fut",
        email="fut@x.com",
        first_name="Fut",
        last_name="Lead",
        extra={"canceled": True},
        raw_subject="Canceled: SalesGlider Intro",
    )
    report = CycleReport()
    reeval_discovery_scheduled(
        hs,
        make_settings(),
        Memory(make_settings(), data_dir=tmp_path),
        report,
        build_timelines([canceled_cal, canceled_fut]),
        upcoming_emails=set(),
        calendar_api_ok=True,
    )
    by_id = {d["id"]: d["properties"]["dealstage"] for d in hs.deals}
    assert by_id["d-cal"] == STAGE["discovery_scheduled"]
    assert by_id["d-fut"] == STAGE["discovery_scheduled"]


def test_review_queue_dedupes_open_person_reason(tmp_path):
    settings = make_settings()
    memory = Memory(settings, data_dir=tmp_path)
    row = {
        "person_key": "email:pat@acme.com",
        "email": "pat@acme.com",
        "name": "Pat Lee",
        "reason": "poc_hint",
    }
    memory.enqueue_review(row)
    memory.enqueue_review(dict(row))
    assert len(memory._local["review_queue"]) == 1
    memory._local["review_queue"][0]["status"] = "done"
    memory.enqueue_review(dict(row))
    assert len(memory._local["review_queue"]) == 2
