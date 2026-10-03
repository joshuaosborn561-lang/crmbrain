"""FastAPI service for Slack interactivity. Separate Railway service from the cron cycle."""

from __future__ import annotations

import json
import logging
from urllib.parse import parse_qs

from fastapi import BackgroundTasks, FastAPI, Request, Response

from crmbrain.config import Settings
from crmbrain.memory import Memory
from crmbrain.nurture import ACTION_EDIT, verify_slack_signature
from crmbrain.nurture_actions import handle_block_action, handle_view_submission, open_edit_modal

logger = logging.getLogger(__name__)

app = FastAPI(title="crmbrain nurture slack", docs_url=None, redoc_url=None)


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


@app.post("/slack/interactions")
async def slack_interactions(request: Request, background_tasks: BackgroundTasks) -> Response:
    raw = await request.body()
    settings = _settings()
    ts = request.headers.get("X-Slack-Request-Timestamp", "")
    sig = request.headers.get("X-Slack-Signature", "")
    if not verify_slack_signature(settings.slack_signing_secret, ts, raw, sig):
        return Response(status_code=401, content="invalid signature")
    try:
        payload = _parse_payload(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return Response(status_code=400, content="bad payload")
    # views.open trigger_id expires in 3s — open the edit modal before returning.
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
    background_tasks.add_task(process_interaction, payload)
    return Response(status_code=200)
