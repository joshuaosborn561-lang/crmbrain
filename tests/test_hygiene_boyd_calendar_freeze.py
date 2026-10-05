"""Oct 5 hygiene: do not archive John Boyd when Calendar 403s or Josh edited after freeze."""

from datetime import datetime, timezone

from crmbrain.config import STAGE
from crmbrain.deal_write import authorize_deal_lifecycle, authorize_deal_write
from crmbrain.evidence import build_timelines
from crmbrain.memory import Memory
from crmbrain.models import CycleReport, Engagement
from crmbrain.policy import (
    deal_has_post_freeze_manual_edit,
    last_manual_modification,
)
from crmbrain.reconcile import apply_timeline, calendar_source_unreliable, run as reconcile_run
from tests.test_crm_gating import FakeHubSpot, make_settings

FREEZE = datetime(2026, 10, 3, 1, 30, tzinfo=timezone.utc)
POST_FREEZE_FORECAST = datetime(2026, 10, 4, 18, 0, tzinfo=timezone.utc)
BOYD_EMAIL = "jboyd@boydsoftx.com"
# Restored live deal. Tests must not recreate 351836593902 or 352630865631.
TEST_DEAL_ID = "boyd-hygiene-test"


def _forecast_history(when: datetime, value: str = "BEST_CASE") -> dict:
    return {
        "hs_manual_forecast_category": [
            {
                "value": value,
                "timestamp": when.isoformat(),
                "sourceType": "CRM_UI",
                "updatedByUserId": "josh",
            }
        ]
    }


def _boyd_deal(*, forecast_at: datetime | None = None, amount: str = "") -> dict:
    props = {
        "dealstage": STAGE["meeting_booked"],
        "dealname": "John Boyd",
        "hs_manual_forecast_category": "BEST_CASE",
        "amount": amount,
    }
    if forecast_at:
        props["manual_modified_at"] = forecast_at.isoformat()
    deal = {
        "id": TEST_DEAL_ID,
        "contact_id": "c-boyd",
        "properties": props,
    }
    if forecast_at:
        deal["propertiesWithHistory"] = _forecast_history(forecast_at)
    return deal


def _boyd_contact() -> dict:
    return {
        "id": "c-boyd",
        "properties": {
            "email": BOYD_EMAIL,
            "firstname": "John",
            "lastname": "Boyd",
            "company": "Boyd Software",
            "crm_source": "smartlead",
        },
    }


def _boyd_reply() -> Engagement:
    return Engagement(
        source="smartlead",
        external_id="sl-boyd-reply",
        occurred_at=datetime(2026, 10, 2, 16, 0, tzinfo=timezone.utc),
        email=BOYD_EMAIL,
        first_name="John",
        last_name="Boyd",
        company="Boyd Software",
        summary="Interested",
    )


def _boyd_setup(tmp_path, *, forecast_at=None):
    settings = make_settings(manual_freeze_at=FREEZE)
    hs = FakeHubSpot([_boyd_contact()])
    hs.deals.append(_boyd_deal(forecast_at=forecast_at))
    hs.calendar_api_ok = True
    timeline = build_timelines([_boyd_reply()])[f"email:{BOYD_EMAIL}"]
    timeline.contact = hs.contacts[0]
    timeline.deals = list(hs.deals)
    return settings, hs, timeline, Memory(settings, data_dir=tmp_path)


def _proposed_archives(report: CycleReport) -> list[dict]:
    return [w for w in report.proposed_writes if w.get("action") == "archive"]


def test_calendar_403_is_unreliable():
    report = CycleReport(calendar_api_ok=False)
    report.warnings.append("calendar: Calendar API 403")
    hs = FakeHubSpot()
    hs.calendar_api_ok = False
    hs.calendar_api_error = "calendar api 403"
    assert calendar_source_unreliable(False, hs=hs, report=report) is True
    assert calendar_source_unreliable(True) is False


def test_last_manual_modification_reads_forecast_category():
    deal = {
        "properties": {"hs_manual_forecast_category": "BEST_CASE"},
        "propertiesWithHistory": _forecast_history(POST_FREEZE_FORECAST),
    }
    when = last_manual_modification(deal)
    assert when == POST_FREEZE_FORECAST
    settings = make_settings(manual_freeze_at=FREEZE)
    assert deal_has_post_freeze_manual_edit(deal, settings) is True
    assert deal_has_post_freeze_manual_edit(_boyd_deal(), settings) is False


def test_calendar_403_reply_only_does_not_propose_archive(tmp_path):
    settings, hs, timeline, memory = _boyd_setup(tmp_path)
    hs.calendar_api_ok = False
    hs.calendar_api_error = "HttpError 403: Calendar API"
    report = CycleReport(calendar_api_ok=False)
    report.warnings.append("calendar: Calendar API 403")
    apply_timeline(
        timeline,
        settings,
        hs,
        memory,
        report,
        calendar_api_ok=False,
        dry_run=True,
    )
    assert hs.deals[0]["id"] == TEST_DEAL_ID
    assert hs.deals[0]["properties"]["dealstage"] == STAGE["meeting_booked"]
    assert _proposed_archives(report) == []
    assert not any("reply-only" in x for x in report.deals_pruned)
    assert any("calendar_unavailable" in x for x in report.review_queue)
    assert any("calendar unavailable" in x for x in report.skipped)


def test_post_freeze_forecast_edit_blocks_archive_and_stage(tmp_path):
    settings, hs, timeline, memory = _boyd_setup(tmp_path, forecast_at=POST_FREEZE_FORECAST)
    deal = hs.deals[0]
    ev = _boyd_reply()
    report = CycleReport()
    assert authorize_deal_lifecycle(deal, ev=ev, settings=settings, action="archive") == (
        False,
        "post_freeze_manual_edit",
    )
    stage, amount, reason = authorize_deal_write(
        ev,
        requested_stage=STAGE["nurture"],
        amount="1",
        deal=deal,
        settings=settings,
    )
    assert stage == ""
    assert amount == ""
    assert reason == "frozen"
    apply_timeline(timeline, settings, hs, memory, report, dry_run=True)
    assert hs.deals[0]["id"] == TEST_DEAL_ID
    assert hs.deals[0]["properties"]["hs_manual_forecast_category"] == "BEST_CASE"
    assert _proposed_archives(report) == []
    assert any("post_freeze_manual_edit" in x for x in report.review_queue + report.skipped)


def test_boyd_calendar_403_and_post_freeze_forecast_dry_run_does_not_archive(tmp_path):
    """Reproduce Mon Oct 5 7am: Calendar 403 + Josh Best case after freeze."""
    settings, hs, timeline, memory = _boyd_setup(tmp_path, forecast_at=POST_FREEZE_FORECAST)
    hs.calendar_api_ok = False
    hs.calendar_api_error = "calendar api 403"
    report = CycleReport(calendar_api_ok=False, dry_run=True)
    report.stale_sources.append("calendar stale-source warning (calendar api 403)")
    reconcile_run(
        settings,
        hs,
        memory,
        report,
        [_boyd_reply()],
        upcoming_emails=set(),
        dry_run=True,
        calendar_api_ok=False,
        skip_abort=True,
    )
    assert {d["id"] for d in hs.deals} == {TEST_DEAL_ID}
    assert hs.deals[0]["properties"]["dealstage"] == STAGE["meeting_booked"]
    assert hs.deals[0]["properties"]["hs_manual_forecast_category"] == "BEST_CASE"
    assert _proposed_archives(report) == []
    assert not any("reply-only" in x for x in report.deals_pruned)
    assert any(
        "calendar_unavailable" in x or "post_freeze_manual_edit" in x for x in report.review_queue
    )


def test_review_queue_row_includes_phone(tmp_path):
    settings = make_settings()
    memory = Memory(settings, data_dir=tmp_path)
    memory.enqueue_review(
        {
            "person_key": "phone:+15125551212",
            "phone": "+15125551212",
            "name": "Unknown Cube",
            "reason": "unknown_phone",
        }
    )
    assert memory._local["review_queue"][0]["phone"] == "+15125551212"


def test_healthy_calendar_still_archives_true_reply_only(tmp_path):
    settings = make_settings()
    hs = FakeHubSpot([_boyd_contact()])
    hs.deals.append(_boyd_deal())
    timeline = build_timelines([_boyd_reply()])[f"email:{BOYD_EMAIL}"]
    timeline.contact = hs.contacts[0]
    timeline.deals = list(hs.deals)
    report = CycleReport()
    apply_timeline(
        timeline,
        settings,
        hs,
        Memory(settings, data_dir=tmp_path),
        report,
        calendar_api_ok=True,
    )
    assert hs.deals == []
    assert any("reply-only" in x for x in report.deals_pruned)
