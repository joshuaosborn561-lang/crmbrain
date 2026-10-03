from __future__ import annotations

from typing import Any

import requests

from crmbrain.config import Settings

SLACK_API = "https://slack.com/api"


def _headers(settings: Settings) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.slack_token}",
        "Content-Type": "application/json",
    }


def post(settings: Settings, text: str) -> dict[str, Any]:
    if not settings.slack_token:
        return {}
    resp = requests.post(
        f"{SLACK_API}/chat.postMessage",
        headers=_headers(settings),
        json={"channel": settings.slack_channel, "text": text},
        timeout=20,
    )
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"slack: {data}")
    return data


def post_blocks(settings: Settings, text: str, blocks: list[dict], channel: str = "") -> dict[str, Any]:
    """Post a Block Kit card to #nurture. Returns Slack chat.postMessage payload."""
    if not settings.slack_token:
        return {}
    resp = requests.post(
        f"{SLACK_API}/chat.postMessage",
        headers=_headers(settings),
        json={
            "channel": channel or settings.slack_channel,
            "text": text,
            "blocks": blocks,
        },
        timeout=20,
    )
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"slack: {data}")
    return data


def update_message(
    settings: Settings,
    channel: str,
    ts: str,
    text: str,
    blocks: list[dict] | None = None,
) -> dict[str, Any]:
    if not settings.slack_token or not channel or not ts:
        return {}
    payload: dict[str, Any] = {"channel": channel, "ts": ts, "text": text}
    if blocks is not None:
        payload["blocks"] = blocks
    resp = requests.post(
        f"{SLACK_API}/chat.update",
        headers=_headers(settings),
        json=payload,
        timeout=20,
    )
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"slack update: {data}")
    return data


def open_modal(settings: Settings, trigger_id: str, view: dict) -> dict[str, Any]:
    if not settings.slack_token or not trigger_id:
        return {}
    resp = requests.post(
        f"{SLACK_API}/views.open",
        headers=_headers(settings),
        json={"trigger_id": trigger_id, "view": view},
        timeout=20,
    )
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"slack views.open: {data}")
    return data
