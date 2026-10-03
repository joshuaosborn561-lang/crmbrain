"""FastAPI service for Slack interactivity. Separate Railway service from the cron cycle."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import parse_qs

from fastapi import BackgroundTasks, FastAPI, Request, Response

from crmbrain.config import DEFAULT_LEGACY_SLACK_INTERACTIONS_URL, Settings
from crmbrain.memory import Memory
from crmbrain.nurture import ACTION_EDIT, verify_slack_signature
from crmbrain.nurture_actions import handle_block_action, handle_view_submission, open_edit_modal

logger = logging.getLogger(__name__)

app = FastAPI(title="crmbrain nurture slack", docs_url=None, redoc_url=None)

NURTURE_ID_PREFIX = "nurture_"
LEGACY_FORWARD_WAIT_SECONDS = 2.5
SLACK_FORWARD_HEADER_NAMES = (
    "X-Slack-Signature",
    "X-Slack-Request-Timestamp",
    "Content-Type",
)


def _settings() -> Settings:
    return Settings.from_env()


@app.get("/health")
def health() -> dict[str, str]:
    return {"ok": "nurture"}


def _parse_payload(raw: bytes) -> dict:
    text = raw.decode("utf-8") if raw else ""
    if text.startswith("{"):
        return json.loads(text)
    form = parse_qs(text, keep_blank_values=True)
    blob = (form.get("payload") or ["{}"])[0]
    return json.loads(blob)


def _header(headers: Mapping[str, str], name: str) -> str:
    wanted = name.lower()
    for key, value in headers.items():
        if str(key).lower() == wanted:
            return "" if value is None else str(value)
    return ""


def slack_forward_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Only the Slack signature, timestamp, and content-type, names unchanged."""
    out: dict[str, str] = {}
    for name in SLACK_FORWARD_HEADER_NAMES:
        value = _header(headers, name)
        if value:
            out[name] = value
    return out


def is_nurture_interaction(payload: dict | None) -> bool:
    """True only for namespaced nurture_* action_id / callback_id values."""
    if not isinstance(payload, dict):
        return False
    for action in payload.get("actions") or []:
        if str((action or {}).get("action_id") or "").startswith(NURTURE_ID_PREFIX):
            return True
    view = payload.get("view") if isinstance(payload.get("view"), dict) else {}
    if str((view or {}).get("callback_id") or "").startswith(NURTURE_ID_PREFIX):
        return True
    if str(payload.get("callback_id") or "").startswith(NURTURE_ID_PREFIX):
        return True
    return False


def _default_legacy_post(url: str, raw: bytes, headers: dict[str, str], timeout: float):
    import requests

    return requests.post(url, data=raw, headers=headers, timeout=timeout)


def _is_timeout(exc: BaseException) -> bool:
    name = type(exc).__name__.lower()
    return "timeout" in name or "timed out" in str(exc).lower()


def forward_legacy_interaction(
    url: str,
    raw: bytes,
    headers: Mapping[str, str],
    *,
    timeout: float = LEGACY_FORWARD_WAIT_SECONDS,
    http_post: Callable[..., Any] | None = None,
) -> tuple[int, bytes, str] | None:
    """POST the raw Slack body to the legacy service. None if it does not return in time."""
    dest = (url or "").strip() or DEFAULT_LEGACY_SLACK_INTERACTIONS_URL
    fwd = slack_forward_headers(headers)
    post = http_post or _default_legacy_post
    try:
        resp = post(dest, raw, fwd, timeout)
    except Exception as exc:
        if _is_timeout(exc):
            logger.warning("legacy slack forward timed out after %.2fs", timeout)
            return None
        logger.warning("legacy slack forward failed: %s", exc)
        return None
    if resp is None:
        return None
    if isinstance(resp, tuple):
        status, body, content_type = resp
        return int(status), body if isinstance(body, (bytes, bytearray)) else str(body or "").encode(), str(content_type or "")
    status = int(getattr(resp, "status_code", 200) or 200)
    content = getattr(resp, "content", b"")
    if isinstance(content, str):
        body = content.encode("utf-8")
    elif isinstance(content, (bytes, bytearray)):
        body = bytes(content)
    else:
        body = b""
    hdrs = getattr(resp, "headers", {}) or {}
    content_type = str(hdrs.get("Content-Type") or hdrs.get("content-type") or "")
    return status, body, content_type


def process_interaction(payload: dict) -> None:
    settings = _settings()
    memory = Memory(settings)
    kind = payload.get("type")
    try:
        if kind == "block_actions":
            handle_block_action(settings, memory, payload)
        elif kind == "view_submission":
            handle_view_submission(settings, memory, payload)
    except Exception:
        logger.exception("nurture interaction failed")


def _handle_nurture(
    payload: dict,
    settings: Settings,
    *,
    background_add: Callable[[Any], None] | None = None,
) -> Response:
    if payload.get("type") == "block_actions":
        actions = payload.get("actions") or []
        action_id = (actions[0] or {}).get("action_id") if actions else ""
        if action_id == ACTION_EDIT:
            ticker_id = str((actions[0] or {}).get("value") or "")
            try:
                channel = str((payload.get("channel") or {}).get("id") or "")
                ts = str((payload.get("message") or {}).get("ts") or "")
                open_edit_modal(
                    settings,
                    Memory(settings),
                    ticker_id,
                    str(payload.get("trigger_id") or ""),
                    channel=channel,
                    ts=ts,
                )
            except Exception:
                logger.exception("nurture modal open failed")
            return Response(status_code=200)
    if background_add is not None:
        background_add(payload)
    else:
        process_interaction(payload)
    return Response(status_code=200)


def _legacy_response(
    raw: bytes,
    headers: Mapping[str, str],
    settings: Settings,
    *,
    http_post: Callable[..., Any] | None = None,
) -> Response:
    relayed = forward_legacy_interaction(
        getattr(settings, "legacy_slack_interactions_url", "") or DEFAULT_LEGACY_SLACK_INTERACTIONS_URL,
        raw,
        headers,
        http_post=http_post,
    )
    if relayed is None:
        return Response(status_code=200)
    status, body, content_type = relayed
    kwargs: dict[str, Any] = {"status_code": status, "content": body}
    if content_type:
        kwargs["media_type"] = content_type
    return Response(**kwargs)


def dispatch_slack_interaction(
    raw: bytes,
    headers: Mapping[str, str],
    settings: Settings,
    *,
    background_add: Callable[[Any], None] | None = None,
    http_post: Callable[..., Any] | None = None,
) -> Response:
    """Route nurture_* locally (after HMAC). Everything else goes to the legacy URL."""
    payload: dict | None
    try:
        payload = _parse_payload(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = None
    if payload is not None and is_nurture_interaction(payload):
        ts = _header(headers, "X-Slack-Request-Timestamp")
        sig = _header(headers, "X-Slack-Signature")
        if not verify_slack_signature(settings.slack_signing_secret, ts, raw, sig):
            return Response(status_code=401, content="invalid signature")
        return _handle_nurture(payload, settings, background_add=background_add)
    return _legacy_response(raw, headers, settings, http_post=http_post)


@app.post("/slack/interactions")
async def slack_interactions(request: Request, background_tasks: BackgroundTasks) -> Response:
    raw = await request.body()
    settings = _settings()
    return dispatch_slack_interaction(
        raw,
        request.headers,
        settings,
        background_add=lambda payload: background_tasks.add_task(process_interaction, payload),
    )
