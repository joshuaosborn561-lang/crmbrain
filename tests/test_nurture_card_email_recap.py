"""Nurture cards show the email, keep it after send, and never fake Re:."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from crmbrain.memory import Memory
from crmbrain.nurture import (
    ACTION_APPROVE,
    ACTION_EDIT,
    ACTION_REMOVE,
    build_nurture_card,
    card_context_line,
    card_people_header,
    compose_nurture_draft,
    compose_nurture_subject,
    nurture_subject,
    outcome_blocks,
    thread_reply_headers,
)
from crmbrain.nurture_actions import handle_block_action, remove_from_nurture, send_nurture_reply
from tests.test_crm_gating import make_settings
from tests.test_nurture_rebuild import FakeGmail, FakeSlack

CDT = ZoneInfo("America/Chicago")
SENT_AT = datetime(2026, 10, 8, 17, 23, tzinfo=CDT)


def _roofing_row(**extra) -> dict:
    row = {
        "id": "t-mike",
        "name": "Mike Dolan",
        "email": "mike@roofrivercity.test",
        "company": "Roof River City",
        "industry": "roofing",
        "reason": "met",
        "met": True,
        "source": "hubspot",
        "meeting_at": "2026-04-02T17:00:00+00:00",
        "hs_deal_id": "351000111",
        "last_touch_snippet": "",
        "status": "active",
        "nurture_state": "queued",
    }
    row.update(extra)
    return row


def _card_texts(card: dict) -> str:
    parts = [card.get("text") or ""]
    for block in card.get("blocks") or []:
        if block.get("type") == "header":
            parts.append((block.get("text") or {}).get("text") or "")
        elif block.get("type") == "section":
            parts.append((block.get("text") or {}).get("text") or "")
        elif block.get("type") == "context":
            for el in block.get("elements") or []:
                parts.append(el.get("text") or "")
        elif block.get("type") == "actions":
            for el in block.get("elements") or []:
                parts.append((el.get("text") or {}).get("text") or "")
    return "\n".join(parts)


def test_new_thread_subject_has_no_re_and_reply_does():
    row = _roofing_row()
    assert compose_nurture_subject(row) == "Roof River City follow up"
    assert not compose_nurture_subject(row).lower().startswith("re:")
    assert nurture_subject("Re: Roof River City follow up", reply=False) == "Roof River City follow up"
    reply = _roofing_row(
        nurture_thread_id="th-nurture-mike",
        nurture_thread_subject="Roof River City follow up",
        in_reply_to="<mike-orig@mail>",
    )
    assert compose_nurture_subject(reply) == "Re: Roof River City follow up"
    headers = thread_reply_headers(
        "Roof River City follow up",
        in_reply_to="<mike-orig@mail>",
        thread_id="th-nurture-mike",
    )
    assert headers["Subject"] == "Re: Roof River City follow up"
    assert thread_reply_headers("Empire Roofing follow up")["Subject"] == "Empire Roofing follow up"


def test_card_layout_shows_full_email_and_buttons():
    row = _roofing_row()
    draft = compose_nurture_draft(row)
    card = build_nurture_card(row, draft)
    blob = _card_texts(card)
    assert card["blocks"][0]["type"] == "header"
    assert card["blocks"][0]["text"]["text"] == "Nurture email to Mike Dolan (Roof River City)"
    assert card["blocks"][1]["type"] == "context"
    assert "mike@roofrivercity.test" in card["blocks"][1]["elements"][0]["text"]
    assert "we already met" in card["blocks"][1]["elements"][0]["text"]
    assert "Last: call Apr 2" in card["blocks"][1]["elements"][0]["text"]
    assert "never_booked" not in blob
    assert card["blocks"][2]["type"] == "divider"
    preview = card["blocks"][3]["text"]["text"]
    assert preview.startswith("*Email that will send*")
    assert "*To:* mike@roofrivercity.test" in preview
    assert "*Subject:* Roof River City follow up" in preview
    assert "Re:" not in preview
    assert f"```{draft.body}```" in preview
    assert draft.body in card["text"]
    actions = next(b for b in card["blocks"] if b["type"] == "actions")
    labels = [el["text"]["text"] for el in actions["elements"]]
    assert labels == ["Approve & send", "Edit & send", "Remove from nurture"]
    assert actions["elements"][0]["style"] == "primary"
    assert {el["action_id"] for el in actions["elements"]} == {ACTION_APPROVE, ACTION_EDIT, ACTION_REMOVE}
    assert "Source: hubspot" not in blob
    assert "They said" not in blob
    assert " / -" not in blob
    footer = card["blocks"][-1]
    assert footer["type"] == "context"
    assert footer["elements"][0]["text"] == "HubSpot deal 351000111"


def test_roofing_card_snapshot():
    row = _roofing_row()
    draft = compose_nurture_draft(row)
    card = build_nurture_card(row, draft)
    assert draft.subject == "Roof River City follow up"
    assert card["blocks"] == [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "Nurture email to Mike Dolan (Roof River City)", "emoji": True},
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "mike@roofrivercity.test · we already met · Last: call Apr 2",
                }
            ],
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "*Email that will send*\n"
                    "*To:* mike@roofrivercity.test\n"
                    "*Subject:* Roof River City follow up\n\n"
                    f"```{draft.body}```"
                ),
            },
        },
        {
            "type": "actions",
            "block_id": "nurture_actions_t-mike",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Approve & send", "emoji": True},
                    "style": "primary",
                    "action_id": ACTION_APPROVE,
                    "value": "t-mike",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Edit & send", "emoji": True},
                    "action_id": ACTION_EDIT,
                    "value": "t-mike",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Remove from nurture", "emoji": True},
                    "style": "danger",
                    "action_id": ACTION_REMOVE,
                    "value": "t-mike",
                },
            ],
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "HubSpot deal 351000111"}],
        },
    ]


def test_send_new_thread_keeps_plain_subject_and_email_on_card(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("crmbrain.nurture_actions.now_utc", lambda: SENT_AT.astimezone(timezone.utc))
    settings = make_settings(nurture_send_enabled=True)
    memory = Memory(settings, data_dir=tmp_path)
    memory._local["ticker"] = [_roofing_row()]
    gmail = FakeGmail()
    slack = FakeSlack()
    out = send_nurture_reply(
        settings,
        memory,
        "t-mike",
        gmail=gmail,
        slack=slack,
        channel="C0BHBDTMRFY",
        ts="1.2",
        actor_user_id="U123JOSH",
    )
    assert out["ok"] is True
    assert out["subject"] == "Roof River City follow up"
    assert gmail.sent[0]["subject"] == "Roof River City follow up"
    assert gmail.sent[0]["threadId"] == ""
    assert gmail.sent[0]["in_reply_to"] == ""
    updated = slack.updated[0]
    blob = "\n".join((b.get("text") or {}).get("text") or "" for b in updated["blocks"] if b.get("type") == "section")
    assert "Sent 5:23pm CT by <@U123JOSH> from Josh's Gmail (new email)" in blob
    assert "*To:* mike@roofrivercity.test" in blob
    assert "*Subject:* Roof River City follow up" in blob
    assert gmail.sent[0]["body"] in blob
    assert "Approve & send" not in blob


def test_send_reply_uses_re_and_thread_reply_label(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("crmbrain.nurture_actions.now_utc", lambda: SENT_AT.astimezone(timezone.utc))
    settings = make_settings(nurture_send_enabled=True)
    memory = Memory(settings, data_dir=tmp_path)
    memory._local["ticker"] = [
        _roofing_row(
            nurture_thread_id="th-nurture-mike",
            nurture_thread_subject="Roof River City follow up",
            in_reply_to="<mike-orig@mail>",
            references="<mike-orig@mail>",
        )
    ]
    gmail = FakeGmail()
    slack = FakeSlack()
    out = send_nurture_reply(
        settings,
        memory,
        "t-mike",
        gmail=gmail,
        slack=slack,
        channel="C",
        ts="9",
        actor_user_id="U123JOSH",
    )
    assert out["subject"] == "Re: Roof River City follow up"
    assert gmail.sent[0]["subject"] == "Re: Roof River City follow up"
    assert gmail.sent[0]["threadId"] == "th-nurture-mike"
    blob = "\n".join((b.get("text") or {}).get("text") or "" for b in slack.updated[0]["blocks"])
    assert "from Josh's Gmail (thread reply)" in blob


def test_edit_send_shows_edited_email(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("crmbrain.nurture_actions.now_utc", lambda: SENT_AT.astimezone(timezone.utc))
    settings = make_settings(nurture_send_enabled=True)
    memory = Memory(settings, data_dir=tmp_path)
    memory._local["ticker"] = [_roofing_row()]
    slack = FakeSlack()
    edited_body = compose_nurture_draft(_roofing_row()).body.replace(
        "following up on our Apr 2 call", "circling back on the April walkthrough"
    )
    out = send_nurture_reply(
        settings,
        memory,
        "t-mike",
        subject="Roof River City follow up",
        body=edited_body,
        gmail=FakeGmail(),
        slack=slack,
        channel="C",
        ts="3",
        action="edit",
        actor_user_id="U123JOSH",
    )
    assert out["ok"] is True
    blob = "\n".join((b.get("text") or {}).get("text") or "" for b in slack.updated[0]["blocks"])
    assert "circling back on the April walkthrough" in blob
    assert "Sent 5:23pm CT by <@U123JOSH> from Josh's Gmail (new email)" in blob


def test_remove_keeps_no_send_status(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("crmbrain.nurture_actions.now_utc", lambda: SENT_AT.astimezone(timezone.utc))
    settings = make_settings()
    memory = Memory(settings, data_dir=tmp_path)
    draft = compose_nurture_draft(_roofing_row())
    memory._local["ticker"] = [_roofing_row(draft_subject=draft.subject, draft_body=draft.body)]
    slack = FakeSlack()
    out = remove_from_nurture(
        settings, memory, "t-mike", slack=slack, channel="C", ts="4", actor_user_id="U123JOSH"
    )
    assert out["outcome"] == "removed"
    blob = "\n".join((b.get("text") or {}).get("text") or "" for b in slack.updated[0]["blocks"])
    assert "Removed by <@U123JOSH> 5:23pm CT; no email sent" in blob
    assert draft.body in blob


def test_block_action_passes_slack_user(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("crmbrain.nurture_actions.now_utc", lambda: SENT_AT.astimezone(timezone.utc))
    settings = make_settings(nurture_send_enabled=True)
    memory = Memory(settings, data_dir=tmp_path)
    memory._local["ticker"] = [_roofing_row()]
    slack = FakeSlack()
    handle_block_action(
        settings,
        memory,
        {
            "type": "block_actions",
            "user": {"id": "U99"},
            "channel": {"id": "C"},
            "message": {"ts": "5"},
            "actions": [{"action_id": ACTION_APPROVE, "value": "t-mike"}],
        },
        gmail=FakeGmail(),
        slack=slack,
    )
    blob = "\n".join((b.get("text") or {}).get("text") or "" for b in slack.updated[0]["blocks"])
    assert "<@U99>" in blob


def test_outcome_blocks_sent_and_removed_copy():
    row = _roofing_row()
    draft = compose_nurture_draft(row)
    sent = outcome_blocks(
        row,
        "sent",
        subject=draft.subject,
        body=draft.body,
        to=row["email"],
        thread_kind="new_thread",
        actor_user_id="U1",
        now=SENT_AT,
    )
    sent_text = "\n".join((b.get("text") or {}).get("text") or "" for b in sent)
    assert sent[0]["text"]["text"] == card_people_header(row)
    assert "Sent 5:23pm CT by <@U1> from Josh's Gmail (new email)" in sent_text
    assert draft.body in sent_text
    removed = outcome_blocks(
        row,
        "removed",
        subject=draft.subject,
        body=draft.body,
        to=row["email"],
        actor_user_id="U1",
        now=SENT_AT,
    )
    removed_text = "\n".join((b.get("text") or {}).get("text") or "" for b in removed)
    assert "Removed by <@U1> 5:23pm CT; no email sent" in removed_text


def test_plain_why_and_header_helpers():
    assert "never_booked" not in card_context_line({"reason": "never_booked", "email": "a@b.test"})
    assert "no meeting yet" in card_context_line({"reason": "never_booked", "email": "a@b.test"})
    assert card_people_header({"name": "Lionel Francis", "company": "Empire Roofing"}) == (
        "Nurture email to Lionel Francis (Empire Roofing)"
    )
