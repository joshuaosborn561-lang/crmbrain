"""Cycle enrolls HubSpot Nurture deals; Slack success owns the daily cap."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from crmbrain.__main__ import _wants_nurture_enroll_dry_run
from crmbrain.config import STAGE
from crmbrain.cycle import _fire_ticker
from crmbrain.memory import Memory
from crmbrain.models import CycleReport
from crmbrain.nurture import (
    apply_legacy_nurture_reset,
    collect_hubspot_nurture_rows,
    count_posted_today,
    enroll_hubspot_nurture_deals,
    fire_due_rows,
    legacy_nurture_reset_candidates,
    nurture_enroll_dry_run_text,
)
from tests.test_crm_gating import make_settings
from tests.test_hard_exclude_and_opener_guard import _FakeNurtureHS
from tests.test_nurture_rebuild import FakeGmail, FakeSlack, _due_nurture_row

CDT = ZoneInfo("America/Chicago")
NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)


class _EnrollHS(_FakeNurtureHS):
    def __init__(self):
        super().__init__()
        self.deals["nurture"].extend(
            [
                {
                    "id": "d-kevin",
                    "properties": {
                        "dealname": "Kevin Hagemoser",
                        "dealstage": STAGE["nurture"],
                        "pipeline": "default",
                        "createdate": "2026-04-01T00:00:00Z",
                    },
                },
            ]
        )
        self.contacts["d-kevin"] = [
            {
                "id": "c-kevin",
                "properties": {
                    "firstname": "Kevin",
                    "lastname": "Hagemoser",
                    "email": "kevin@kevinhagemoser.com",
                    "company": "Hagemoser",
                },
            }
        ]


class _FailSlack(FakeSlack):
    def post_blocks(self, settings, text, blocks):
        raise RuntimeError("slack 500")


class _EmptySlack(FakeSlack):
    def post_blocks(self, settings, text, blocks):
        return {}


def test_collect_matches_v11_sample_eligibility():
    settings = make_settings()
    collected = collect_hubspot_nurture_rows(settings, hs=_EnrollHS(), gmail=FakeGmail())
    emails = {r["email"] for r in collected["rows"]}
    assert "jackie@kellyroofing.com" in emails
    assert "kevin@kevinhagemoser.com" not in emails
    assert "jeremy.ciotola@gmail.com" not in emails
    assert "dave@goliath.com" not in emails
    companies = [r.get("company") for r in collected["rows"] if "kelly" in str(r.get("company") or "").lower()]
    assert len(companies) == 1
    assert collected["skipped"].get("non_deal")
    assert collected["skipped"].get("closed_won")


def test_enroll_one_card_per_company():
    hs = _EnrollHS()
    hs.deals["nurture"].append(
        {
            "id": "d-jackie-2",
            "properties": {
                "dealname": "Pat Kelly - Kelly Roofing",
                "dealstage": STAGE["nurture"],
                "pipeline": "default",
                "createdate": "2026-04-11T00:00:00Z",
                "description": "Check back after our busy season.",
            },
        }
    )
    hs.contacts["d-jackie-2"] = [
        {
            "id": "c-pat",
            "properties": {
                "firstname": "Pat",
                "lastname": "Kelly",
                "email": "pat@kellyroofing.com",
                "company": "Kelly Roofing",
                "personal_details": "Check back after our busy season.",
            },
        }
    ]
    collected = collect_hubspot_nurture_rows(make_settings(), hs=hs, gmail=FakeGmail())
    kelly = [r for r in collected["rows"] if "kelly" in str(r.get("company") or "").lower()]
    assert len(kelly) == 1
    assert collected["skipped"].get("same_company") == 1


def test_cycle_enroll_writes_hubspot_rows_due_now(tmp_path: Path):
    settings = make_settings()
    memory = Memory(settings, data_dir=tmp_path)
    report = CycleReport()
    result = enroll_hubspot_nurture_deals(
        settings, memory, report, hs=_EnrollHS(), gmail=FakeGmail(), now=NOW, write=True
    )
    assert result["enrolled"] >= 1
    rows = [t for t in memory._local["ticker"] if t.get("source") == "hubspot"]
    assert rows
    jackie = next(r for r in rows if r.get("email") == "jackie@kellyroofing.com")
    assert jackie["source"] == "hubspot"
    assert jackie["source_ref"] == "d-keep"
    assert jackie["hs_deal_id"] == "d-keep"
    assert jackie["next_fire_at"] == NOW.isoformat()
    assert report.ticker_enrolled
    again = enroll_hubspot_nurture_deals(
        settings, memory, CycleReport(), hs=_EnrollHS(), gmail=FakeGmail(), now=NOW, write=True
    )
    assert again["skipped"].get("already_enrolled")
    assert len([t for t in memory._local["ticker"] if t.get("email") == "jackie@kellyroofing.com"]) == 1


def test_last_sent_at_cooldown_not_enrollment(tmp_path: Path):
    settings = make_settings()
    memory = Memory(settings, data_dir=tmp_path)
    sent = NOW - timedelta(days=10)
    memory._local["ticker"] = [
        {
            "id": "old-jackie",
            "email": "jackie@kellyroofing.com",
            "hs_contact_id": "c-jackie",
            "hs_deal_id": "d-keep",
            "status": "stopped",
            "stop_reason": "legacy_reset",
            "last_sent_at": sent.isoformat(),
        }
    ]
    result = enroll_hubspot_nurture_deals(
        settings, memory, CycleReport(), hs=_EnrollHS(), gmail=FakeGmail(), now=NOW, write=True
    )
    jackie = next(r for r in result["rows"] if r.get("email") == "jackie@kellyroofing.com")
    assert jackie["next_fire_at"] == (sent + timedelta(days=90)).isoformat()
    assert jackie["next_fire_at"] != NOW.isoformat()


def test_enroll_dry_run_prints_counts_and_next_five(tmp_path: Path):
    settings = make_settings()
    memory = Memory(settings, data_dir=tmp_path)
    result = enroll_hubspot_nurture_deals(
        settings, memory, hs=_EnrollHS(), gmail=FakeGmail(), now=NOW, write=False
    )
    assert memory._local.get("ticker") in (None, [])
    text = nurture_enroll_dry_run_text(result)
    assert "Eligible:" in text
    assert "Would post tomorrow (first 5):" in text
    assert "jackie@kellyroofing.com" in text
    wanted, rest = _wants_nurture_enroll_dry_run(["cycle", "--nurture-enroll-dry-run"])
    assert wanted is True
    assert rest == ["cycle"]


def test_slack_failure_or_post_off_does_not_count_toward_cap(tmp_path: Path):
    settings = make_settings(nurture_post_enabled=True)
    memory = Memory(settings, data_dir=tmp_path)
    memory._local["ticker"] = [_due_nurture_row(0)]
    monday = datetime(2026, 10, 6, 7, 0, tzinfo=CDT)
    cards = fire_due_rows(
        settings, memory, CycleReport(), now=monday, gmail=FakeGmail(), slack=_FailSlack()
    )
    assert cards
    assert count_posted_today(memory, now=monday) == 0
    assert not memory.get_ticker("t-cap-0").get("last_fired_at")
    empty = fire_due_rows(
        settings, memory, CycleReport(), now=monday, gmail=FakeGmail(), slack=_EmptySlack()
    )
    assert empty
    assert count_posted_today(memory, now=monday) == 0
    off = make_settings(nurture_post_enabled=False)
    off_cards = fire_due_rows(off, memory, CycleReport(), now=monday, gmail=FakeGmail())
    assert off_cards
    assert count_posted_today(memory, now=monday) == 0


def test_successful_slack_post_sets_last_fired_and_counts_cap(tmp_path: Path):
    settings = make_settings(nurture_post_enabled=True)
    memory = Memory(settings, data_dir=tmp_path)
    memory._local["ticker"] = [_due_nurture_row(0)]
    monday = datetime(2026, 10, 6, 7, 0, tzinfo=CDT)
    fire_due_rows(settings, memory, CycleReport(), now=monday, gmail=FakeGmail(), slack=FakeSlack())
    row = memory.get_ticker("t-cap-0")
    assert row["last_fired_at"]
    assert row["slack_ts"]
    assert row["slack_channel"]
    assert count_posted_today(memory, now=monday) == 1


def test_legacy_reset_skips_nurture_mapped_and_is_not_in_cycle(tmp_path: Path):
    settings = make_settings()
    memory = Memory(settings, data_dir=tmp_path)
    memory._local["ticker"] = [
        {"id": "leg-1", "email": "a@x.test", "status": "active", "source": None, "reason": "never_booked"},
        {"id": "keep-hs", "email": "b@x.test", "status": "active", "source": "hubspot", "hs_deal_id": "d-keep"},
        {
            "id": "keep-nurture",
            "email": "c@x.test",
            "status": "active",
            "source": None,
            "hs_deal_id": "d-keep",
        },
    ]
    dry = apply_legacy_nurture_reset(memory, nurture_deal_ids={"d-keep"}, write=False)
    assert dry["count"] == 1
    assert dry["ids"] == ["leg-1"]
    assert memory.get_ticker("leg-1")["status"] == "active"
    applied = apply_legacy_nurture_reset(memory, nurture_deal_ids={"d-keep"}, write=True)
    assert applied["wrote"] is True
    assert memory.get_ticker("leg-1")["status"] == "stopped"
    assert memory.get_ticker("leg-1")["stop_reason"] == "legacy_reset"
    assert memory.get_ticker("keep-hs")["status"] == "active"
    assert memory.get_ticker("keep-nurture")["status"] == "active"
    assert legacy_nurture_reset_candidates(memory.list_ticker(), {"d-keep"}) == []
    source = Path("crmbrain/cycle.py").read_text()
    assert "apply_legacy_nurture_reset" not in source
    assert "legacy_reset" not in source


def test_fire_ticker_still_weekend_safe(tmp_path: Path):
    settings = make_settings(nurture_post_enabled=True)
    memory = Memory(settings, data_dir=tmp_path)
    memory._local["ticker"] = [_due_nurture_row(0)]
    _fire_ticker(
        settings,
        memory,
        CycleReport(),
        now=datetime(2026, 10, 3, 7, 0, tzinfo=CDT),
    )
    assert count_posted_today(memory, now=datetime(2026, 10, 3, 7, 0, tzinfo=CDT)) == 0
