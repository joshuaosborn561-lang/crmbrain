"""Nurture vs legacy Slack interactivity routing and forwarding."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from urllib.parse import quote

from crmbrain.config import DEFAULT_LEGACY_SLACK_INTERACTIONS_URL, Settings
from crmbrain.nurture import ACTION_APPROVE, ACTION_EDIT, ACTION_REMOVE, VIEW_EDIT
from crmbrain.nurture_app import (
    NURTURE_ID_PREFIX,
    dispatch_slack_interaction,
    forward_legacy_interaction,
    is_nurture_interaction,
    slack_forward_headers,
)
from tests.test_nurture_rebuild import make_settings


def _sign(secret: str, raw: bytes, ts: str | None = None) -> tuple[str, str]:
    ts = ts or str(int(datetime.now(timezone.utc).timestamp()))
    digest = "v0=" + hmac.new(secret.encode(), b"v0:" + ts.encode() + b":" + raw, hashlib.sha256).hexdigest()
    return ts, digest


def _form(payload: dict) -> bytes:
    return f"payload={quote(json.dumps(payload))}".encode()


def test_is_nurture_interaction_namespaces_action_and_callback_ids():
    assert NURTURE_ID_PREFIX == "nurture_"
    assert is_nurture_interaction({"type": "block_actions", "actions": [{"action_id": ACTION_APPROVE}]})
    assert is_nurture_interaction({"type": "block_actions", "actions": [{"action_id": ACTION_EDIT}]})
    assert is_nurture_interaction({"type": "block_actions", "actions": [{"action_id": ACTION_REMOVE}]})
    assert is_nurture_interaction({"type": "view_submission", "view": {"callback_id": VIEW_EDIT}})
    assert is_nurture_interaction({"callback_id": "nurture_shortcut"})
    assert not is_nurture_interaction({"type": "block_actions", "actions": [{"action_id": "approve"}]})
    assert not is_nurture_interaction({"type": "block_actions", "actions": [{"action_id": "edit"}]})
    assert not is_nurture_interaction({"type": "block_actions", "actions": [{"action_id": "reject"}]})
    assert not is_nurture_interaction({"type": "view_submission", "view": {"callback_id": "fireflies_edit"}})
    assert not is_nurture_interaction({"type": "block_actions", "actions": []})
    assert not is_nurture_interaction({})
    assert not is_nurture_interaction(None)


def test_slack_forward_headers_pass_only_signature_timestamp_content_type():
    headers = slack_forward_headers(
        {
            "X-Slack-Signature": "v0=abc",
            "X-Slack-Request-Timestamp": "1710000000",
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": "Bearer should-not-forward",
            "X-Forwarded-For": "1.2.3.4",
        }
    )
    assert headers == {
        "X-Slack-Signature": "v0=abc",
        "X-Slack-Request-Timestamp": "1710000000",
        "Content-Type": "application/x-www-form-urlencoded",
    }


def test_forward_legacy_posts_raw_body_and_canonical_headers():
    calls: list[tuple] = []

    def http_post(url, raw, headers, timeout):
        calls.append((url, raw, headers, timeout))

        class Resp:
            status_code = 200
            content = b'{"response_action":"update"}'
            headers = {"Content-Type": "application/json"}

        return Resp()

    raw = b"payload=%7B%22type%22%3A%22view_submission%22%7D"
    out = forward_legacy_interaction(
        "",
        raw,
        {
            "x-slack-signature": "v0=sig",
            "x-slack-request-timestamp": "99",
            "content-type": "application/x-www-form-urlencoded",
        },
        http_post=http_post,
    )
    assert out == (200, b'{"response_action":"update"}', "application/json")
    assert calls[0][0] == DEFAULT_LEGACY_SLACK_INTERACTIONS_URL
    assert calls[0][1] is raw
    assert calls[0][2] == {
        "X-Slack-Signature": "v0=sig",
        "X-Slack-Request-Timestamp": "99",
        "Content-Type": "application/x-www-form-urlencoded",
    }


def test_legacy_block_action_forwards_without_local_signature():
    settings = make_settings(slack_signing_secret="test-signing-secret")
    payload = {"type": "block_actions", "actions": [{"action_id": "approve", "value": "old-1"}]}
    raw = _form(payload)
    calls: list[tuple] = []

    def http_post(url, body, headers, timeout):
        calls.append((url, body, headers, timeout))

        class Resp:
            status_code = 200
            content = b""
            headers = {}

        return Resp()

    handled: list[dict] = []
    resp = dispatch_slack_interaction(
        raw,
        {"Content-Type": "application/x-www-form-urlencoded"},
        settings,
        background_add=handled.append,
        http_post=http_post,
    )
    assert resp.status_code == 200
    assert handled == []
    assert calls and calls[0][1] == raw
    assert "X-Slack-Signature" not in calls[0][2]


def test_legacy_view_submission_relays_response_body():
    settings = make_settings()
    payload = {
        "type": "view_submission",
        "view": {"callback_id": "legacy_edit_modal", "state": {"values": {}}},
    }
    raw = _form(payload)
    body = b'{"response_action":"errors","errors":{"subject":"required"}}'

    def http_post(url, _raw, headers, timeout):
        del url, _raw, headers, timeout

        class Resp:
            status_code = 200
            content = body
            headers = {"Content-Type": "application/json"}

        return Resp()

    resp = dispatch_slack_interaction(raw, {"Content-Type": "application/x-www-form-urlencoded"}, settings, http_post=http_post)
    assert resp.status_code == 200
    assert resp.body == body
    assert "application/json" in (resp.media_type or "")


def test_legacy_timeout_still_acks_slack():
    settings = make_settings()
    raw = _form({"type": "block_actions", "actions": [{"action_id": "reject"}]})

    def http_post(url, body, headers, timeout):
        del url, body, headers, timeout
        raise TimeoutError("read timed out")

    resp = dispatch_slack_interaction(raw, {}, settings, http_post=http_post)
    assert resp.status_code == 200
    assert resp.body in {b"", b"null"} or resp.body == b""


def test_nurture_action_requires_valid_signature_and_is_not_forwarded():
    settings = make_settings(slack_signing_secret="test-signing-secret")
    payload = {"type": "block_actions", "actions": [{"action_id": ACTION_APPROVE, "value": "t-1"}]}
    raw = _form(payload)
    forwarded: list = []
    handled: list[dict] = []

    def http_post(*args, **kwargs):
        forwarded.append((args, kwargs))
        raise AssertionError("nurture must not forward")

    bad = dispatch_slack_interaction(
        raw,
        {"X-Slack-Signature": "v0=nope", "X-Slack-Request-Timestamp": "1"},
        settings,
        background_add=handled.append,
        http_post=http_post,
    )
    assert bad.status_code == 401
    assert handled == []
    assert forwarded == []

    ts, sig = _sign("test-signing-secret", raw)
    ok = dispatch_slack_interaction(
        raw,
        {
            "X-Slack-Signature": sig,
            "X-Slack-Request-Timestamp": ts,
            "Content-Type": "application/x-www-form-urlencoded",
        },
        settings,
        background_add=handled.append,
        http_post=http_post,
    )
    assert ok.status_code == 200
    assert handled and handled[0]["actions"][0]["action_id"] == ACTION_APPROVE
    assert forwarded == []


def test_nurture_view_submission_is_local_not_legacy():
    settings = make_settings(slack_signing_secret="test-signing-secret")
    raw = _form({"type": "view_submission", "view": {"callback_id": VIEW_EDIT}})
    ts, sig = _sign("test-signing-secret", raw)
    handled: list[dict] = []
    resp = dispatch_slack_interaction(
        raw,
        {"X-Slack-Signature": sig, "X-Slack-Request-Timestamp": ts},
        settings,
        background_add=handled.append,
        http_post=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no forward")),
    )
    assert resp.status_code == 200
    assert handled[0]["view"]["callback_id"] == VIEW_EDIT


def test_unparseable_payload_forwards_to_legacy():
    settings = make_settings()
    raw = b"not-a-payload"
    calls: list[bytes] = []

    def http_post(url, body, headers, timeout):
        del url, headers, timeout
        calls.append(body)

        class Resp:
            status_code = 200
            content = b"ok"
            headers = {}

        return Resp()

    resp = dispatch_slack_interaction(raw, {"Content-Type": "text/plain"}, settings, http_post=http_post)
    assert resp.status_code == 200
    assert calls == [raw]


def test_legacy_url_env_override(monkeypatch):
    monkeypatch.setenv("LEGACY_SLACK_INTERACTIONS_URL", "https://legacy.example.test/slack/interactions")
    settings = Settings.from_env()
    assert settings.legacy_slack_interactions_url == "https://legacy.example.test/slack/interactions"
    monkeypatch.delenv("LEGACY_SLACK_INTERACTIONS_URL", raising=False)
    settings = Settings.from_env()
    assert settings.legacy_slack_interactions_url == DEFAULT_LEGACY_SLACK_INTERACTIONS_URL
