"""Only Josh/Cayden Slack ids may approve, edit, or remove nurture cards."""

from __future__ import annotations

import json
from pathlib import Path

from crmbrain.config import (
    DEFAULT_NURTURE_APPROVER_IDS,
    Settings,
    parse_nurture_approver_ids,
)
from crmbrain.memory import Memory
from crmbrain.nurture import ACTION_APPROVE, ACTION_EDIT, ACTION_REMOVE, VIEW_EDIT, compose_nurture_draft
from crmbrain.nurture_actions import (
    NURTURE_APPROVER_DENIED,
    handle_block_action,
    handle_view_submission,
    is_nurture_approver,
)
from crmbrain.nurture_app import dispatch_slack_interaction
from tests.test_nurture_card_email_recap import _roofing_row
from tests.test_nurture_rebuild import FakeGmail, FakeSlack, make_settings
from tests.test_nurture_slack_relay import _form, _sign

JOSH = "U0AAX2XFJE7"
CAYDEN = "U0BL8JT75KN"
STRANGER = "U99NOTALLOWED"


def _row(**extra) -> dict:
    return _roofing_row(**extra)


def _block_payload(action_id: str, user_id: str, ticker_id: str = "t-mike") -> dict:
    return {
        "type": "block_actions",
        "user": {"id": user_id},
        "channel": {"id": "C0BHBDTMRFY"},
        "message": {"ts": "111.222"},
        "trigger_id": "trig.1",
        "actions": [{"action_id": action_id, "value": ticker_id}],
    }


def _edit_view_payload(user_id: str, ticker_id: str = "t-mike", *, subject: str = "", body: str = "") -> dict:
    return {
        "type": "view_submission",
        "user": {"id": user_id},
        "view": {
            "callback_id": VIEW_EDIT,
            "private_metadata": json.dumps(
                {"ticker_id": ticker_id, "channel": "C0BHBDTMRFY", "ts": "111.222"}
            ),
            "state": {
                "values": {
                    "nurture_subject": {"subject": {"value": subject}},
                    "nurture_body": {"body": {"value": body}},
                }
            },
        },
    }


def test_default_approver_ids_are_josh_and_cayden():
    assert DEFAULT_NURTURE_APPROVER_IDS == (JOSH, CAYDEN)
    assert parse_nurture_approver_ids(None) == (JOSH, CAYDEN)
    assert parse_nurture_approver_ids("") == (JOSH, CAYDEN)
    assert parse_nurture_approver_ids("  ,  ") == (JOSH, CAYDEN)
    assert parse_nurture_approver_ids(f" {JOSH} , {CAYDEN} ") == (JOSH, CAYDEN)
    assert parse_nurture_approver_ids("UONLYONE") == ("UONLYONE",)


def test_from_env_reads_nurture_approver_ids(monkeypatch):
    monkeypatch.delenv("NURTURE_APPROVER_IDS", raising=False)
    settings = Settings.from_env()
    assert settings.nurture_approver_ids == (JOSH, CAYDEN)
    monkeypatch.setenv("NURTURE_APPROVER_IDS", " UCUSTOM1 , UCUSTOM2 ")
    settings = Settings.from_env()
    assert settings.nurture_approver_ids == ("UCUSTOM1", "UCUSTOM2")


def test_is_nurture_approver_uses_settings_allowlist():
    settings = make_settings()
    assert is_nurture_approver(settings, JOSH)
    assert is_nurture_approver(settings, CAYDEN)
    assert not is_nurture_approver(settings, STRANGER)
    assert not is_nurture_approver(settings, "")
    assert not is_nurture_approver(settings, "  ")
    narrowed = make_settings(nurture_approver_ids=(STRANGER,))
    assert is_nurture_approver(narrowed, STRANGER)
    assert not is_nurture_approver(narrowed, JOSH)


def _assert_denied(out, slack, memory, ticker_id="t-mike"):
    assert out == {"ok": False, "outcome": "denied", "reason": "not_approver"}
    assert slack.ephemeral == [
        {
            "channel": "C0BHBDTMRFY",
            "user": STRANGER,
            "text": NURTURE_APPROVER_DENIED,
        }
    ]
    assert slack.updated == []
    assert slack.modals == []
    row = memory.get_ticker(ticker_id) or {}
    assert row.get("nurture_state") == "queued"


def test_stranger_approve_is_ephemeral_and_does_not_send(tmp_path: Path):
    settings = make_settings(nurture_send_enabled=True)
    memory = Memory(settings, data_dir=tmp_path)
    memory._local["ticker"] = [_row()]
    slack = FakeSlack()
    gmail = FakeGmail()
    out = handle_block_action(
        settings,
        memory,
        _block_payload(ACTION_APPROVE, STRANGER),
        gmail=gmail,
        slack=slack,
    )
    _assert_denied(out, slack, memory)
    assert gmail.sent == []


def test_stranger_edit_does_not_open_modal(tmp_path: Path):
    settings = make_settings(nurture_send_enabled=True)
    memory = Memory(settings, data_dir=tmp_path)
    memory._local["ticker"] = [_row()]
    slack = FakeSlack()
    out = handle_block_action(
        settings,
        memory,
        _block_payload(ACTION_EDIT, STRANGER),
        slack=slack,
    )
    _assert_denied(out, slack, memory)


def test_stranger_remove_does_not_change_state(tmp_path: Path):
    settings = make_settings()
    memory = Memory(settings, data_dir=tmp_path)
    memory._local["ticker"] = [_row()]
    slack = FakeSlack()
    out = handle_block_action(
        settings,
        memory,
        _block_payload(ACTION_REMOVE, STRANGER),
        slack=slack,
    )
    _assert_denied(out, slack, memory)
    row = memory.get_ticker("t-mike") or {}
    assert row.get("stop_reason") in {None, ""}


def test_stranger_edit_modal_submit_does_not_send(tmp_path: Path):
    settings = make_settings(nurture_send_enabled=True)
    memory = Memory(settings, data_dir=tmp_path)
    memory._local["ticker"] = [_row()]
    slack = FakeSlack()
    gmail = FakeGmail()
    out = handle_view_submission(
        settings,
        memory,
        _edit_view_payload(STRANGER),
        gmail=gmail,
        slack=slack,
    )
    _assert_denied(out, slack, memory)
    assert gmail.sent == []


def test_josh_and_cayden_may_approve(tmp_path: Path):
    settings = make_settings(nurture_send_enabled=True)
    for user_id in (JOSH, CAYDEN):
        memory = Memory(settings, data_dir=tmp_path / user_id)
        memory._local["ticker"] = [_row()]
        slack = FakeSlack()
        gmail = FakeGmail()
        out = handle_block_action(
            settings,
            memory,
            _block_payload(ACTION_APPROVE, user_id),
            gmail=gmail,
            slack=slack,
        )
        assert out["ok"] is True
        assert out["outcome"] == "sent"
        assert slack.ephemeral == []
        assert gmail.sent
        assert (memory.get_ticker("t-mike") or {}).get("nurture_state") == "sent"


def test_josh_may_open_edit_modal_and_cayden_may_submit(tmp_path: Path):
    settings = make_settings(nurture_send_enabled=True)
    memory = Memory(settings, data_dir=tmp_path)
    memory._local["ticker"] = [_row()]
    slack = FakeSlack()
    opened = handle_block_action(
        settings,
        memory,
        _block_payload(ACTION_EDIT, JOSH),
        slack=slack,
    )
    assert opened["outcome"] == "modal"
    assert slack.modals and slack.ephemeral == []
    draft = compose_nurture_draft(_row())
    sent = handle_view_submission(
        settings,
        memory,
        _edit_view_payload(CAYDEN, subject=draft.subject, body=draft.body),
        gmail=FakeGmail(),
        slack=slack,
    )
    assert sent["ok"] is True
    assert sent["outcome"] == "sent"
    assert (memory.get_ticker("t-mike") or {}).get("nurture_state") == "sent"


def test_dispatch_denies_stranger_before_edit_modal_or_background(monkeypatch):
    settings = make_settings(slack_signing_secret="test-signing-secret")
    payload = _block_payload(ACTION_EDIT, STRANGER)
    raw = _form(payload)
    ts, sig = _sign("test-signing-secret", raw)
    slack = FakeSlack()
    handled: list[dict] = []
    opened: list = []

    def boom_modal(*_a, **_k):
        opened.append(1)
        raise AssertionError("stranger must not open the edit modal")

    monkeypatch.setattr("crmbrain.nurture_app.open_edit_modal", boom_modal)
    resp = dispatch_slack_interaction(
        raw,
        {"X-Slack-Signature": sig, "X-Slack-Request-Timestamp": ts},
        settings,
        background_add=handled.append,
        http_post=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no forward")),
        slack=slack,
    )
    assert resp.status_code == 200
    assert handled == []
    assert opened == []
    assert slack.ephemeral[0]["text"] == NURTURE_APPROVER_DENIED
    assert slack.ephemeral[0]["user"] == STRANGER


def test_dispatch_denies_stranger_view_submission_without_background():
    settings = make_settings(slack_signing_secret="test-signing-secret")
    raw = _form(_edit_view_payload(STRANGER))
    ts, sig = _sign("test-signing-secret", raw)
    slack = FakeSlack()
    handled: list[dict] = []
    resp = dispatch_slack_interaction(
        raw,
        {"X-Slack-Signature": sig, "X-Slack-Request-Timestamp": ts},
        settings,
        background_add=handled.append,
        http_post=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no forward")),
        slack=slack,
    )
    assert resp.status_code == 200
    assert handled == []
    assert slack.ephemeral[0]["text"] == NURTURE_APPROVER_DENIED


def test_empty_user_id_is_denied(tmp_path: Path):
    settings = make_settings(nurture_send_enabled=True)
    memory = Memory(settings, data_dir=tmp_path)
    memory._local["ticker"] = [_row()]
    slack = FakeSlack()
    gmail = FakeGmail()
    out = handle_block_action(
        settings,
        memory,
        _block_payload(ACTION_APPROVE, ""),
        gmail=gmail,
        slack=slack,
    )
    assert out["outcome"] == "denied"
    assert gmail.sent == []
    assert slack.ephemeral[0]["text"] == NURTURE_APPROVER_DENIED
    assert (memory.get_ticker("t-mike") or {}).get("nurture_state") == "queued"
