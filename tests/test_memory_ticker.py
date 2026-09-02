import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

from crmbrain.config import STAGE, Settings
from crmbrain.cycle import _fire_ticker, integration_status, run as cycle_run
from crmbrain.memory import Memory
from crmbrain.models import CycleReport
from crmbrain.ticker import (
    TickerCandidate,
    already_enrolled,
    apply_plan,
    classify_reason,
    next_fire_from_signal,
)


def make_settings(**kwargs) -> Settings:
    base = dict(
        hubspot_token="",
        gmail_client_id="",
        gmail_client_secret="",
        gmail_refresh_token="",
        josh_brief_email="joshua@salesglidergrowth.com",
        fireflies_key="",
        smartlead_key="",
        cube_folder="folder",
        heyreach_url="https://mcp.heyreach.io/mcp",
        heyreach_key="",
        heyreach_campaign_id=1,
        heyreach_linkedin_account_id=1,
        enrichment_url="",
        enrichment_client_tag="salesglider",
        leadmagic_key="",
        slack_token="",
        slack_channel="C0BHBDTMRFY",
        supabase_url="https://example.supabase.co",
        supabase_key="",
        gemini_key="",
        gemini_model="gemini-2.5-flash",
        allo_url="",
        allo_key="",
        lookback_hours=36,
    )
    base.update(kwargs)
    return Settings(**base)


def _boom(*_a, **_k):
    raise RuntimeError("supabase crmbrain.ticker 404: The schema must be one of the following: public")


def test_memory_records_supabase_errors_on_report(tmp_path: Path):
    settings = make_settings(supabase_key="super-secret-key")
    memory = Memory(settings, data_dir=tmp_path)
    assert memory.use_supabase
    memory._sb_schema = _boom

    memory.mark_processed("smartlead", "lead-1")
    memory.enroll_ticker(
        {
            "id": "t1",
            "email": "pat@example.com",
            "status": "active",
            "next_fire_at": "2020-01-01T00:00:00+00:00",
        }
    )
    assert memory.start_run() is None
    memory.finish_run(9, "ok", {"errors": []})
    due = memory.due_ticker("2026-01-01T00:00:00+00:00")
    assert due
    assert any(row.get("email") == "pat@example.com" for row in due)

    report = CycleReport()
    report.errors.extend(memory.drain_errors())
    text = report.summary_text()
    assert "errors:" in text
    assert "memory mark_processed" in text
    assert "memory enroll_ticker" in text
    assert "memory start_run" in text
    assert "memory finish_run" in text
    assert "memory due_ticker" in text
    assert "super-secret-key" not in text
    assert "smartlead:lead-1" in memory._local["processed"]
    assert memory.drain_errors() == []


def test_cycle_summary_includes_integrations_and_memory_errors(tmp_path: Path, monkeypatch):
    settings = make_settings(
        supabase_key="super-secret-key",
        hubspot_token="",
        slack_token="xoxb-secret",
    )
    monkeypatch.setattr("crmbrain.cycle.Memory", lambda s: Memory(s, data_dir=tmp_path))

    original_init = Memory.__init__

    def init_and_break(self, s, data_dir=None):
        original_init(self, s, data_dir=data_dir or tmp_path)
        self._sb_schema = _boom

    monkeypatch.setattr(Memory, "__init__", init_and_break)
    report = cycle_run(settings)
    text = report.summary_text()
    assert "HubSpot: missing" in text
    assert "Gmail: missing" in text
    assert "Supabase key: present" in text
    assert "Slack token: present" in text
    assert "HUBSPOT_ACCESS_TOKEN missing" in text
    assert "memory start_run" in text
    assert "super-secret-key" not in text
    assert "xoxb-secret" not in text


def test_integration_status_present_missing_only():
    settings = make_settings(
        hubspot_token="hs-secret-value",
        gmail_client_id="gmail-client-id-value",
        gmail_client_secret="gmail-client-secret-value",
        gmail_refresh_token="gmail-refresh-token-value",
        fireflies_key="ff-secret-value",
        cube_folder="",
    )
    rows = integration_status(settings)
    assert "HubSpot: present" in rows
    assert "Gmail: present" in rows
    assert "Fireflies: present" in rows
    assert "Smartlead: missing" in rows
    assert "HeyReach key: missing" in rows
    assert "Slack token: missing" in rows
    assert "Supabase key: missing" in rows
    assert "Cube folder: missing" in rows
    blob = " ".join(rows)
    assert "hs-secret-value" not in blob
    assert "gmail-client-secret-value" not in blob
    assert "gmail-refresh-token-value" not in blob
    assert "ff-secret-value" not in blob


def test_next_fire_from_signal_future_and_past():
    now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    last = now - timedelta(days=10)
    assert next_fire_from_signal(last, now) == (last + timedelta(days=90)).isoformat()
    stale = now - timedelta(days=100)
    assert next_fire_from_signal(stale, now) == now.isoformat()
    assert next_fire_from_signal(now - timedelta(days=90), now) == now.isoformat()


def test_classify_reason_and_already_enrolled():
    assert classify_reason(stage=STAGE["no_show"]) == "no_show"
    assert classify_reason(text="Let's circle back next quarter") == "kicked_can"
    assert classify_reason(hint="never_booked") == "never_booked"
    assert classify_reason() == "never_booked"
    rows = [
        {"email": "Pat@Example.com", "status": "stopped", "hs_contact_id": "99"},
        {"phone": "+15551212", "status": "active"},
    ]
    assert already_enrolled(rows, email="pat@example.com")
    assert already_enrolled(rows, hs_contact_id="99")
    assert not already_enrolled(rows, email="other@example.com")


def test_backfill_dry_run_does_not_write(tmp_path: Path):
    settings = make_settings()
    memory = Memory(settings, data_dir=tmp_path)
    now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    memory._local["ticker"] = [{"email": "skip@example.com", "status": "active"}]
    candidates = [
        TickerCandidate(
            name="Ann",
            email="ann@example.com",
            company="Ann Co",
            reason="never_booked",
            last_signal=now - timedelta(days=100),
            source="smartlead",
        ),
        TickerCandidate(
            name="Bob",
            email="bob@example.com",
            company="Bob Co",
            reason="no_show",
            last_signal=now - timedelta(days=10),
            source="hubspot",
        ),
        TickerCandidate(
            name="Skip",
            email="skip@example.com",
            reason="never_booked",
            last_signal=now,
            source="smartlead",
        ),
    ]
    dry = apply_plan(memory, candidates, now=now, write=False)
    assert dry["would_enroll"] == 2
    assert dry["enrolled"] == 0
    assert dry["already_on_ticker"] == 1
    assert dry["by_reason"]["never_booked"] == 1
    assert dry["by_reason"]["no_show"] == 1
    assert dry["wrote"] is False
    assert memory._local["ticker"] == [{"email": "skip@example.com", "status": "active"}]
    ann = next(r for r in dry["rows"] if r["email"] == "ann@example.com")
    assert ann["next_fire_at"] == now.isoformat()
    bob = next(r for r in dry["rows"] if r["email"] == "bob@example.com")
    assert bob["next_fire_at"] == (now - timedelta(days=10) + timedelta(days=90)).isoformat()

    written = apply_plan(memory, candidates, now=now, write=True)
    assert written["enrolled"] == 2
    emails = {t.get("email") for t in memory._local["ticker"]}
    assert emails == {"skip@example.com", "ann@example.com", "bob@example.com"}


def test_fire_ticker_posts_subject_and_body_for_approval(tmp_path: Path, monkeypatch):
    posted: list[str] = []
    monkeypatch.setattr("crmbrain.slack_notify.post", lambda _settings, text: posted.append(text))
    settings = make_settings()
    memory = Memory(settings, data_dir=tmp_path)
    memory._local["ticker"] = [
        {
            "id": "t-roof",
            "name": "Jackie Darkazalli",
            "email": "jackie@kellyroofing.com",
            "company": "Kelly Roofing",
            "reason": "kicked_can",
            "status": "active",
            "next_fire_at": "2020-01-01T00:00:00+00:00",
        }
    ]
    report = CycleReport()
    _fire_ticker(settings, memory, report)
    assert posted
    text = posted[0]
    assert "90-day ticker (approve before send)" in text
    assert "To: jackie@kellyroofing.com" in text
    assert "Why: kicked_can" in text
    assert "Subject: Roofing?" in text
    assert "Josh Osborn" in text
    assert "$100K" in text
    assert report.ticker_drafts == ["jackie@kellyroofing.com"]


def test_backfill_script_dry_run_prints_counts_without_writing(tmp_path: Path):
    path = Path(__file__).resolve().parents[1] / "scripts" / "backfill_nurture_ticker.py"
    spec = importlib.util.spec_from_file_location("backfill_nurture_ticker", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    settings = make_settings()
    memory = Memory(settings, data_dir=tmp_path)
    result = mod.run(apply=False, settings=settings, memory=memory)
    assert result["would_enroll"] == 0
    assert result["enrolled"] == 0
    assert result["wrote"] is False
    assert "would_enroll: 0" in result["report"]
    assert "Nothing written" in result["report"]
    assert memory._local.get("ticker") == []
