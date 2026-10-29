"""Meeting briefs are retired (EA owns them): send_due is a no-op unless BRIEFS_ENABLED=1."""
from __future__ import annotations

from unittest.mock import MagicMock

from crmbrain import briefing
from crmbrain.config import Settings
from crmbrain.models import CycleReport
from tests.test_crm_gating import make_settings


def test_briefs_default_off_from_env(monkeypatch):
    monkeypatch.delenv("BRIEFS_ENABLED", raising=False)
    assert Settings.from_env().briefs_enabled is False
    monkeypatch.setenv("BRIEFS_ENABLED", "1")
    assert Settings.from_env().briefs_enabled is True
    monkeypatch.setenv("BRIEFS_ENABLED", "0")
    assert Settings.from_env().briefs_enabled is False


def test_send_due_is_noop_when_disabled(monkeypatch):
    scan = MagicMock(side_effect=AssertionError("must not scan calendar when briefs are off"))
    monkeypatch.setattr(briefing, "collect_upcoming", scan)
    gmail = MagicMock()
    report = CycleReport()
    briefing.send_due(make_settings(), gmail, None, MagicMock(), report)
    scan.assert_not_called()
    assert gmail.method_calls == []
    assert report.briefs_sent == []
    assert report.errors == []
    assert any("briefing disabled" in s for s in report.skipped)


def test_send_due_still_scans_when_enabled(monkeypatch):
    scan = MagicMock(return_value=[])
    monkeypatch.setattr(briefing, "collect_upcoming", scan)
    briefing.send_due(make_settings(briefs_enabled=True), MagicMock(), None, MagicMock(), CycleReport())
    scan.assert_called_once()
