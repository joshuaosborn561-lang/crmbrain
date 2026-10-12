"""G7 date numbers are not invented; empty They-said / Source / - stay off the card."""

from __future__ import annotations

from crmbrain.nurture import (
    LANE_PROOF_BANK,
    MEETING_GUARANTEE,
    NURTURE_QUESTION,
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
    assert draft.valid is False
    assert draft.reject_reason in {"no_recap", "no_lane_proof"}
    body = (
        "Hey George, following up on our Apr 30 call.\n\n"
        f"{LANE_PROOF_BANK['roofing']}.\n\n"
        f"{NURTURE_QUESTION}\n\n"
        f"{MEETING_GUARANTEE}\n\n"
        "Josh Osborn"
    )
    ok = validate_draft(NurtureDraft(subject="George follow up", body=body), _george_row())
    assert ok.valid is True
    assert ok.reject_reason == ""


def test_random_47_meetings_claim_is_still_rejected():
    row = _george_row()
    body = (
        "Hey George, circling back on 47 meetings.\n\n"
        f"{LANE_PROOF_BANK['roofing']}.\n\n"
        f"{NURTURE_QUESTION}\n\n"
        f"{MEETING_GUARANTEE}\n\n"
        "Josh Osborn"
    )
    draft = validate_draft(NurtureDraft(subject="George", body=body), row)
    assert draft.valid is False
    assert draft.reject_reason == "invented_number"


def _card_blob(card: dict) -> str:
    parts = [card.get("text") or ""]
    for block in card.get("blocks") or []:
        text = block.get("text")
        if isinstance(text, dict):
            parts.append(text.get("text") or "")
        for el in block.get("elements") or []:
            raw = el.get("text")
            parts.append(raw.get("text") if isinstance(raw, dict) else (raw or ""))
    return "\n".join(parts)


def test_card_omits_empty_they_said_and_bare_hubspot_source():
    row = _george_row()
    draft = compose_nurture_draft(row)
    card = build_nurture_card(row, draft)
    blob = _card_blob(card)
    assert "They said" not in blob
    assert "Source: hubspot" not in blob
    assert " / -" not in blob
    assert "Email that will send" in blob
    assert "Nurture email to George Stradiota" in blob


def test_card_shows_hubspot_deal_id_in_footer_not_they_said():
    row = _george_row(
        hs_deal_id="351112233",
        last_touch_snippet="Check back after our busy season.",
    )
    draft = compose_nurture_draft(row)
    card = build_nurture_card(row, draft)
    blob = _card_blob(card)
    assert "HubSpot deal 351112233" in blob
    assert "Source: hubspot" not in blob
    assert "They said" not in blob
    assert "Email that will send" in blob
