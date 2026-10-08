"""Freeze is HubSpot-write only; skipped ticker rows do not consume daily slots."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from crmbrain.memory import Memory
from crmbrain.models import CycleReport, Engagement
from crmbrain.nurture import (
    TICKER_DAYS,
    count_posted_today,
    enroll_hubspot_nurture_deals,
    fire_due_rows,
    fire_gate,
    row_has_future_meeting,
    stopped_row_blocks_reenroll,
)
from crmbrain.nurture_actions import _blocked_send
from crmbrain.policy import event_predates_freeze
from tests.test_crm_gating import make_settings
from tests.test_nurture_cycle_enroll import _EnrollHS
from tests.test_nurture_rebuild import FakeGmail, FakeSlack, _due_nurture_row

CDT = ZoneInfo("America/Chicago")
FREEZE = datetime(2026, 10, 3, 1, 30, tzinfo=timezone.utc)
MONDAY = datetime(2026, 10, 6, 7, 0, tzinfo=CDT)
NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
PRE_FREEZE = datetime(2026, 9, 15, 14, 0, tzinfo=timezone.utc)


class _FutureMeetingHS:
    def __init__(self, future=True):
        self.calls: list[tuple[str, datetime | None]] = []
        self.future = future

    def contact_has_future_meetings(self, contact_id: str, now=None) -> bool:
        self.calls.append((contact_id, now))
        return bool(self.future)


class _BoomHS:
    def contact_has_future_meetings(self, contact_id: str, now=None) -> bool:
        del contact_id, now
        raise RuntimeError("hubspot 500")


def _freeze_settings(**kwargs):
    base = dict(manual_freeze_at=FREEZE, nurture_post_enabled=True, nurture_max_per_weekday=5)
    base.update(kwargs)
    return make_settings(**base)


def _pre_freeze_row(i: int, **extra) -> dict:
    row = _due_nurture_row(i)
    row["signal_at"] = f"2026-03-{i + 1:02d}T00:00:00+00:00"
    row.update(extra)
    return row


def test_fire_gate_ignores_manual_freeze_for_pre_freeze_rows():
    settings = _freeze_settings()
    row = _pre_freeze_row(0)
    assert parse_before_freeze(row["signal_at"])
    reason, patch = fire_gate(row, settings=settings, now=NOW)
    assert reason == ""
    assert patch == {}


def parse_before_freeze(stamp: str) -> bool:
    ev = Engagement(source="nurture", external_id="t", occurred_at=datetime.fromisoformat(stamp))
    return event_predates_freeze(ev, _freeze_settings())


def test_hubspot_write_freeze_still_applies():
    settings = _freeze_settings()
    before = Engagement(source="fireflies", external_id="ff-old", occurred_at=PRE_FREEZE)
    after = Engagement(
        source="fireflies",
        external_id="ff-new",
        occurred_at=datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc),
    )
    assert event_predates_freeze(before, settings) is True
    assert event_predates_freeze(after, settings) is False
    assert event_predates_freeze(before, make_settings(manual_freeze_at=None)) is False


def test_blocked_send_is_not_blocked_by_freeze():
    settings = _freeze_settings(nurture_send_enabled=True)
    row = _pre_freeze_row(0)
    assert _blocked_send(settings, row) == ""
    locked = _pre_freeze_row(1, crmbrain_locked="true")
    assert _blocked_send(settings, locked) == "error"
    off = _freeze_settings(nurture_send_enabled=False)
    assert _blocked_send(off, row) == "disabled"


def test_eight_pre_freeze_due_rows_post_five_and_roll_three(tmp_path: Path):
    settings = _freeze_settings()
    memory = Memory(settings, data_dir=tmp_path)
    memory._local["ticker"] = [_pre_freeze_row(i) for i in range(8)]
    report = CycleReport()
    cards = fire_due_rows(
        settings, memory, report, now=MONDAY, gmail=FakeGmail(), slack=FakeSlack()
    )
    assert len(cards) == 5
    assert count_posted_today(memory, now=MONDAY) == 5
    rolled = [
        row
        for row in memory._local["ticker"]
        if str(row.get("id") or "").startswith("t-cap-")
        and (row.get("next_fire_at") or "").startswith("2026-10-07")
    ]
    assert len(rolled) == 3
    assert all(not row.get("last_fired_at") for row in rolled)
    assert sum(1 for line in report.ticker_skipped if line.endswith(" rolled")) == 3
    assert all("manual_freeze" not in line for line in report.ticker_skipped)


def test_booked_and_emailed_recently_do_not_consume_slots(tmp_path: Path):
    settings = _freeze_settings()
    memory = Memory(settings, data_dir=tmp_path)
    future = (MONDAY + timedelta(days=4)).isoformat()
    booked_dated = _pre_freeze_row(0, meeting_at=future)
    booked_flag = _pre_freeze_row(1, booked=True)
    emailed = _pre_freeze_row(2, last_sent_at=(NOW - timedelta(days=10)).isoformat())
    eligible = [_pre_freeze_row(i) for i in range(3, 11)]
    memory._local["ticker"] = [booked_dated, booked_flag, emailed, *eligible]
    report = CycleReport()
    cards = fire_due_rows(
        settings, memory, report, now=MONDAY, gmail=FakeGmail(), slack=FakeSlack()
    )
    assert len(cards) == 5
    posted_ids = {c.get("ticker_id") for c in cards}
    assert "t-cap-0" not in posted_ids
    assert "t-cap-1" not in posted_ids
    assert "t-cap-2" not in posted_ids
    assert count_posted_today(memory, now=MONDAY) == 5
    stopped = [memory.get_ticker("t-cap-0"), memory.get_ticker("t-cap-1")]
    assert all(row["status"] == "stopped" for row in stopped)
    assert all(row["stop_reason"] == "booked" for row in stopped)
    emailed_row = memory.get_ticker("t-cap-2")
    assert emailed_row["status"] == "active"
    assert emailed_row["next_fire_at"]
    assert any("booked" in line for line in report.ticker_skipped)
    assert any("emailed_recently" in line for line in report.ticker_skipped)
    rolled = [line for line in report.ticker_skipped if line.endswith(" rolled")]
    assert len(rolled) == 3


def test_past_meeting_at_does_not_stop_row(tmp_path: Path):
    settings = _freeze_settings()
    memory = Memory(settings, data_dir=tmp_path)
    past = (MONDAY - timedelta(days=12)).isoformat()
    row = _pre_freeze_row(
        0,
        meeting_at=past,
        reason="met",
        met=True,
        last_touch_snippet="Check back after our busy season. Sep 24.",
    )
    memory._local["ticker"] = [row]
    cards = fire_due_rows(
        settings, memory, CycleReport(), now=MONDAY, gmail=FakeGmail(), slack=FakeSlack()
    )
    assert len(cards) == 1
    kept = memory.get_ticker("t-cap-0")
    assert kept["status"] == "active"
    assert kept.get("stop_reason") != "booked"
    assert row_has_future_meeting(row, now=MONDAY) is False


def test_g4_uses_hubspot_contact_has_future_meetings(tmp_path: Path):
    settings = _freeze_settings()
    memory = Memory(settings, data_dir=tmp_path)
    row = _pre_freeze_row(0, hs_contact_id="c-booked")
    memory._local["ticker"] = [row]
    hs = _FutureMeetingHS(True)
    report = CycleReport()
    cards = fire_due_rows(
        settings, memory, report, now=MONDAY, gmail=FakeGmail(), slack=FakeSlack(), hs=hs
    )
    assert cards == []
    assert hs.calls == [("c-booked", MONDAY.astimezone(timezone.utc))]
    stopped = memory.get_ticker("t-cap-0")
    assert stopped["status"] == "stopped"
    assert stopped["stop_reason"] == "booked"
    assert any("booked" in line for line in report.ticker_skipped)


def test_hubspot_future_meeting_exception_does_not_stop_row(tmp_path: Path):
    settings = _freeze_settings()
    memory = Memory(settings, data_dir=tmp_path)
    row = _pre_freeze_row(0, hs_contact_id="c-boom")
    memory._local["ticker"] = [row]
    cards = fire_due_rows(
        settings,
        memory,
        CycleReport(),
        now=MONDAY,
        gmail=FakeGmail(),
        slack=FakeSlack(),
        hs=_BoomHS(),
    )
    assert len(cards) == 1
    kept = memory.get_ticker("t-cap-0")
    assert kept["status"] == "active"
    assert kept.get("stop_reason") != "booked"
    assert row_has_future_meeting(row, now=MONDAY, hs=_BoomHS()) is False


def test_stopped_row_blocks_reenroll_rules():
    recent = {
        "status": "stopped",
        "stop_reason": "booked",
        "stopped_at": (NOW - timedelta(days=10)).isoformat(),
    }
    expired = {
        "status": "stopped",
        "stop_reason": "booked",
        "stopped_at": (NOW - timedelta(days=TICKER_DAYS + 1)).isoformat(),
    }
    removed = {"status": "stopped", "stop_reason": "removed", "stopped_at": NOW.isoformat()}
    legacy = {"status": "stopped", "stop_reason": "legacy_reset", "stopped_at": NOW.isoformat()}
    active = {"status": "active", "stop_reason": "booked"}
    assert stopped_row_blocks_reenroll(recent, now=NOW) is True
    assert stopped_row_blocks_reenroll(expired, now=NOW) is False
    assert stopped_row_blocks_reenroll(removed, now=NOW) is True
    assert stopped_row_blocks_reenroll(legacy, now=NOW) is False
    assert stopped_row_blocks_reenroll(active, now=NOW) is False


def test_enroll_does_not_readd_recent_booked_stop(tmp_path: Path):
    settings = _freeze_settings()
    memory = Memory(settings, data_dir=tmp_path)
    memory._local["ticker"] = [
        {
            "id": "old-jackie",
            "email": "jackie@kellyroofing.com",
            "hs_contact_id": "c-jackie",
            "hs_deal_id": "d-keep",
            "status": "stopped",
            "stop_reason": "booked",
            "stopped_at": (NOW - timedelta(days=10)).isoformat(),
        }
    ]
    result = enroll_hubspot_nurture_deals(
        settings, memory, CycleReport(), hs=_EnrollHS(), gmail=FakeGmail(), now=NOW, write=True
    )
    jackies = [t for t in memory._local["ticker"] if t.get("email") == "jackie@kellyroofing.com"]
    assert len(jackies) == 1
    assert jackies[0]["id"] == "old-jackie"
    assert jackies[0]["status"] == "stopped"
    assert result["enrolled"] == 0
    assert result["skipped"].get("stopped")
    assert not result["rows"]
