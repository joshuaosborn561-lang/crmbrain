from __future__ import annotations

import requests

from crmbrain.config import Settings


def post(settings: Settings, text: str) -> None:
    if not settings.slack_token:
        return
    resp = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={
            "Authorization": f"Bearer {settings.slack_token}",
            "Content-Type": "application/json",
        },
        json={"channel": settings.slack_channel, "text": text},
        timeout=20,
    )
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"slack: {data}")
