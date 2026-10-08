"""Approve / edit / remove handlers for #nurture Slack cards."""

from __future__ import annotations

import logging
from typing import Any

from crmbrain.config import Settings, now_utc
from crmbrain.gmail_client import Gmail
from crmbrain.memory import Memory
from crmbrain.nurture import (
    ACTION_APPROVE,
    ACTION_EDIT,
    ACTION_REMOVE,
    VIEW_EDIT,
    attach_gmail_thread,
    compose_nurture_draft,
    persist_nurture_thread,
    cooldown_until,
    edit_modal,
    outcome_blocks,
    validate_draft,
    NurtureDraft,
)
from crmbrain.policy import deal_is_locked

logger = logging.getLogger(__name__)


def _confirm(settings: Settings, slack, channel: str, ts: str, row: dict, outcome: str, detail: str = "") -> None:
    blocks = outcome_blocks(row, outcome, detail)
    text = f"Nurture {outcome}: {row.get('name') or row.get('email')}"
    if slack is None:
        from crmbrain import slack_notify

        slack = slack_notify
    try:
        slack.update_message(settings, channel, ts, text, blocks)
    except Exception as exc:
        logger.warning("nurture slack confirm failed: %s", exc)


def _blocked_send(settings: Settings, row: dict) -> str:
    if not getattr(settings, "nurture_send_enabled", False):
        return "disabled"
    if deal_is_locked({"properties": {"crmbrain_locked": row.get("crmbrain_locked")}}):
        return "error"
    return ""


def send_nurture_reply(
    settings: Settings,
    memory: Memory,
    ticker_id: str,
    *,
    subject: str | None = None,
    body: str | None = None,
    gmail=None,
    hs=None,
    slack=None,
    channel: str = "",
    ts: str = "",
    action: str = "approve",
) -> dict[str, Any]:
    """Idempotent Gmail send. First touch starts a new thread; later ones reply there."""
    claim = memory.claim_nurture_action(ticker_id, action)
    row = memory.get_ticker(ticker_id) or {}
    channel = channel or str(row.get("slack_channel") or settings.slack_channel)
    ts = ts or str(row.get("slack_ts") or "")
    if claim in {"already_sent", "already_removed", "in_progress"}:
        _confirm(settings, slack, channel, ts, row, claim)
        return {"ok": False, "outcome": claim, "ticker_id": ticker_id}
    blocked = _blocked_send(settings, row)
    if blocked:
        memory.patch_ticker(ticker_id, {"nurture_state": "queued"})
        _confirm(settings, slack, channel, ts, row, blocked, "Send blocked.")
        return {"ok": False, "outcome": blocked, "ticker_id": ticker_id}
    to = str(row.get("email") or "")
    if not to:
        memory.patch_ticker(ticker_id, {"nurture_state": "queued"})
        _confirm(settings, slack, channel, ts, row, "error", "Missing email.")
        return {"ok": False, "outcome": "error", "reason": "no_email"}
    client = gmail or Gmail(settings)
    row = attach_gmail_thread(row)
    draft = compose_nurture_draft(row)
    sub = subject if subject is not None else (row.get("draft_subject") or draft.subject)
    bod = body if body is not None else (row.get("draft_body") or draft.body)
    checked = validate_draft(NurtureDraft(subject=sub, body=bod), row)
    if not checked.valid:
        memory.patch_ticker(ticker_id, {"nurture_state": "queued"})
        _confirm(settings, slack, channel, ts, row, "error", f"G7 {checked.reject_reason}")
        return {"ok": False, "outcome": "error", "reason": checked.reject_reason}
    thread_id = str(row.get("nurture_thread_id") or "")
    thread_kind = str(row.get("thread_kind") or ("reply" if thread_id else "new_thread"))
    memory.patch_ticker(
        ticker_id,
        {
            "nurture_thread_id": thread_id or None,
            "nurture_thread_subject": row.get("nurture_thread_subject") or (sub if not thread_id else None),
            "gmail_thread_id": thread_id or None,
            "original_subject": row.get("nurture_thread_subject") or row.get("original_subject") or None,
            "thread_kind": thread_kind,
        },
    )
    try:
        sent = client.send_thread_reply(
            to=to,
            subject=sub,
            body=bod,
            thread_id=thread_id,
            in_reply_to=str(row.get("in_reply_to") or ""),
            references=str(row.get("references") or row.get("in_reply_to") or ""),
        )
    except Exception as exc:
        logger.warning("nurture send failed: %s", exc)
        memory.patch_ticker(ticker_id, {"nurture_state": "queued"})
        _confirm(settings, slack, channel, ts, row, "error", str(exc))
        return {"ok": False, "outcome": "error", "reason": str(exc)}
    stored_id = ""
    if isinstance(sent, dict):
        stored_id = str(sent.get("threadId") or sent.get("thread_id") or thread_id or "")
    stored_id = stored_id or thread_id
    stored_subject = str(row.get("nurture_thread_subject") or sub or "")
    if stored_id:
        persist_nurture_thread(hs, row, stored_id, stored_subject)
        if hs is None and getattr(settings, "hubspot_token", ""):
            try:
                from crmbrain.hubspot import HubSpot

                persist_nurture_thread(HubSpot(settings), row, stored_id, stored_subject)
            except Exception as exc:
                logger.warning("nurture_thread_id hubspot write skipped: %s", exc)
    cool = cooldown_until(now_utc())
    memory.patch_ticker(
        ticker_id,
        {
            "nurture_state": "sent",
            "stop_reason": "emailed_recently",
            "next_fire_at": cool.isoformat(),
            "last_sent_at": now_utc().isoformat(),
            "gmail_message_id": (sent or {}).get("id") if isinstance(sent, dict) else "",
            "nurture_thread_id": stored_id or None,
            "nurture_thread_subject": stored_subject or None,
            "gmail_thread_id": stored_id or None,
            "original_subject": stored_subject or None,
            "thread_kind": "reply" if stored_id else thread_kind,
        },
    )
    _confirm(settings, slack, channel, ts, row, "sent")
    return {
        "ok": True,
        "outcome": "sent",
        "ticker_id": ticker_id,
        "cooldown_until": cool.isoformat(),
        "thread_kind": thread_kind,
    }


def remove_from_nurture(
    settings: Settings,
    memory: Memory,
    ticker_id: str,
    *,
    slack=None,
    channel: str = "",
    ts: str = "",
) -> dict[str, Any]:
    """Permanent hard stop. Idempotent."""
    claim = memory.claim_nurture_action(ticker_id, "remove")
    row = memory.get_ticker(ticker_id) or {}
    channel = channel or str(row.get("slack_channel") or settings.slack_channel)
    ts = ts or str(row.get("slack_ts") or "")
    if claim == "already_removed":
        _confirm(settings, slack, channel, ts, row, "already_removed")
        return {"ok": False, "outcome": "already_removed", "ticker_id": ticker_id}
    if claim == "already_sent":
        _confirm(settings, slack, channel, ts, row, "already_sent")
        return {"ok": False, "outcome": "already_sent", "ticker_id": ticker_id}
    memory.stop_ticker(
        email=row.get("email"),
        hs_contact_id=row.get("hs_contact_id"),
        stop_reason="do_not_contact",
        ticker_id=ticker_id,
    )
    memory.patch_ticker(ticker_id, {"nurture_state": "removed", "stop_reason": "do_not_contact"})
    row = memory.get_ticker(ticker_id) or row
    _confirm(settings, slack, channel, ts, row, "removed")
    return {"ok": True, "outcome": "removed", "ticker_id": ticker_id}


def open_edit_modal(
    settings: Settings,
    memory: Memory,
    ticker_id: str,
    trigger_id: str,
    slack=None,
    channel: str = "",
    ts: str = "",
) -> dict[str, Any]:
    row = memory.get_ticker(ticker_id) or {}
    draft = compose_nurture_draft(row)
    view = edit_modal(row, draft, channel=channel, ts=ts)
    if slack is None:
        from crmbrain import slack_notify

        slack = slack_notify
    slack.open_modal(settings, trigger_id, view)
    return {"ok": True, "outcome": "modal"}


def handle_block_action(
    settings: Settings,
    memory: Memory,
    payload: dict,
    *,
    gmail=None,
    slack=None,
) -> dict[str, Any]:
    actions = payload.get("actions") or []
    if not actions:
        return {"ok": False, "outcome": "no_action"}
    action = actions[0]
    action_id = action.get("action_id")
    ticker_id = str(action.get("value") or "")
    channel = str((payload.get("channel") or {}).get("id") or "")
    ts = str((payload.get("message") or {}).get("ts") or "")
    if action_id == ACTION_EDIT:
        return open_edit_modal(
            settings,
            memory,
            ticker_id,
            str(payload.get("trigger_id") or ""),
            slack=slack,
            channel=channel,
            ts=ts,
        )
    if action_id == ACTION_APPROVE:
        return send_nurture_reply(
            settings, memory, ticker_id, gmail=gmail, slack=slack, channel=channel, ts=ts, action="approve"
        )
    if action_id == ACTION_REMOVE:
        return remove_from_nurture(settings, memory, ticker_id, slack=slack, channel=channel, ts=ts)
    return {"ok": False, "outcome": "unknown_action"}


def handle_view_submission(
    settings: Settings,
    memory: Memory,
    payload: dict,
    *,
    gmail=None,
    slack=None,
) -> dict[str, Any]:
    view = payload.get("view") or {}
    if view.get("callback_id") != VIEW_EDIT:
        return {"ok": False, "outcome": "unknown_view"}
    import json

    meta = {}
    raw_meta = view.get("private_metadata") or "{}"
    try:
        meta = json.loads(raw_meta) if isinstance(raw_meta, str) else dict(raw_meta)
    except (TypeError, ValueError):
        meta = {}
    ticker_id = str(meta.get("ticker_id") or "")
    values = (view.get("state") or {}).get("values") or {}
    subject = ((values.get("nurture_subject") or {}).get("subject") or {}).get("value") or ""
    body = ((values.get("nurture_body") or {}).get("body") or {}).get("value") or ""
    channel = str(meta.get("channel") or "")
    ts = str(meta.get("ts") or "")
    return send_nurture_reply(
        settings,
        memory,
        ticker_id,
        subject=subject,
        body=body,
        gmail=gmail,
        slack=slack,
        channel=channel,
        ts=ts,
        action="edit",
    )
