"""G7 date numbers are not invented; empty They-said / Source / - stay off the card."""

from __future__ import annotations

from crmbrain.nurture import (
    GENERAL_PROOF,
    MEETING_GUARANTEE,
    NurtureDraft,
    build_nurture_card,
    compose_nurture_draft,
    validate_draft,
)


def _george_row(**extra) -> dict:
    row = {
        "id": "t-george",
        "name": "George Stradiota",
        "email": "george@stradiota.test",
        "company": "Stradiota",
        "reason": "met",
        "met": True,
        "source": "hubspot",
        "meeting_at": "2026-04-30T17:00:00+00:00",
        "last_touch_snippet": "",
    }
    row.update(extra)
    return row


def test_george_apr_30_empty_snippet_is_not_invented_number():
    draft = compose_nurture_draft(_george_row())
    opener = draft.body.split("\n", 1)[0]
    assert "Apr 30" in opener
    assert draft.valid is True
    assert draft.reject_reason == ""


def test_random_47_meetings_claim_is_still_rejected():
    row = _george_row()
    body = (
        "Hey George, circling back on 47 meetings.\n\n"
        f"{GENERAL_PROOF}\n\n"
        f"{MEETING_GUARANTEE}\n\n"
        "Worth a look?\n\n"
        "Josh Osborn"
    )
    draft = validate_draft(NurtureDraft(subject="George", body=body), row)
    assert draft.valid is False
    assert draft.reject_reason == "invented_number"


def _card_section(card: dict) -> str:
    return next(b["text"]["text"] for b in card["blocks"] if b.get("type") == "section")


def test_card_omits_empty_they_said_and_bare_hubspot_source():
    row = _george_row()
    draft = compose_nurture_draft(row)
    card = build_nurture_card(row, draft)
    section = _card_section(card)
    assert "They said" not in card["text"]
    assert "They said" not in section
    assert '""' not in card["text"]
    assert "Source: hubspot\n" in card["text"]
    assert "*Source:* hubspot\n" in section
    assert " / -" not in card["text"]
    assert " / -" not in section


def test_card_shows_hubspot_deal_id_and_keeps_snippet():
    row = _george_row(
        hs_deal_id="351112233",
        last_touch_snippet="Check back after our busy season.",
    )
    draft = compose_nurture_draft(row)
    card = build_nurture_card(row, draft)
    section = _card_section(card)
    assert "Source: hubspot / 351112233" in card["text"]
    assert "*Source:* hubspot / 351112233" in section
    assert '*They said:* "Check back after our busy season."' in section
    assert 'They said: "Check back after our busy season."' in card["text"]
