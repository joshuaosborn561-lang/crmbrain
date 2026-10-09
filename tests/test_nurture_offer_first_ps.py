"""Offer-first nurture body: question, approved proof, guarantee, sig, PS."""

from __future__ import annotations

from crmbrain.__main__ import _parse_nurture_preview, _run_nurture_preview
from crmbrain.nurture import (
    CASE_STUDIES,
    GENERAL_PROOF,
    MAX_BODY_WORDS,
    MEETING_GUARANTEE,
    NurtureDraft,
    PS_BURNED,
    PS_BUSY,
    PS_REFERRALS,
    PS_TIMING,
    ROOFING_PROOF,
    THINKING_QUESTIONS,
    TRADES_PROOF,
    build_nurture_card,
    compose_nurture_draft,
    nurture_body_paragraphs,
    strip_ps_tail,
    validate_draft,
)
from crmbrain.ticker import draft_email, has_free_poc_offer
from tests.test_crm_gating import make_settings


def _main_words(body: str) -> list[str]:
    return [w for w in strip_ps_tail(body).split() if w]


def test_offer_first_order_and_band():
    draft = compose_nurture_draft(
        {
            "name": "Pat Reyes",
            "company": "Summit Roofs",
            "industry": "roofing",
            "reason": "met",
            "meeting_at": "2026-04-02T17:00:00+00:00",
            "last_touch_snippet": "Check back after our busy season.",
        }
    )
    parts = nurture_body_paragraphs(draft.body)
    assert len(parts) == 6
    assert parts[0].startswith("Hey Pat, we do done-for-you outbound")
    assert "after our apr 2 call" in parts[0].lower()
    assert parts[1] == THINKING_QUESTIONS["roofing"] or parts[1].endswith("?")
    assert "season slows down" in parts[1].lower() or "jobs this quarter" in parts[1].lower()
    assert parts[2].rstrip(".").lower() == ROOFING_PROOF.rstrip(".").lower()
    assert parts[3] == MEETING_GUARANTEE
    assert parts[4] == "Josh Osborn"
    assert parts[5].startswith("PS:")
    assert draft.valid is True
    assert 90 <= len(_main_words(draft.body)) <= MAX_BODY_WORDS
    assert "AirPods" not in draft.body
    assert "Worth a look?" not in draft.body
    assert "—" not in draft.body
    assert not has_free_poc_offer(draft.body)


def test_thinking_question_and_ps_follow_snippet_or_industry():
    roof = compose_nurture_draft(
        {"name": "Jackie Darkazalli", "company": "Kelly Roofing", "industry": "roofing"}
    )
    assert THINKING_QUESTIONS["roofing"] in roof.body
    assert PS_BURNED in roof.body

    referrals = compose_nurture_draft(
        {
            "name": "Pat Reyes",
            "company": "Summit Roofs",
            "industry": "hvac",
            "last_touch_snippet": "We get enough work from referrals and word of mouth.",
        }
    )
    assert THINKING_QUESTIONS["roofing"] in referrals.body
    assert PS_REFERRALS in referrals.body

    burned = compose_nurture_draft(
        {
            "name": "Dana Ortiz",
            "industry": "hvac",
            "last_touch_snippet": "Last agency burned us. Not doing that again.",
        }
    )
    assert PS_BURNED in burned.body

    timing = compose_nurture_draft(
        {
            "name": "Dana Ortiz",
            "industry": "hvac",
            "last_touch_snippet": "Check back in the fall.",
        }
    )
    assert PS_TIMING in timing.body

    busy = compose_nurture_draft(
        {
            "name": "Dana Ortiz",
            "company": "Lin Holdings",
            "last_touch_snippet": "We are slammed and too busy for more meetings.",
        }
    )
    assert PS_BUSY in busy.body


def test_social_proof_only_uses_approved_case_studies():
    roof = compose_nurture_draft({"name": "Pat Reyes", "company": "Kelly Roofing", "industry": "roofing"})
    hvac = compose_nurture_draft({"name": "Joel Stewart", "company": "The Chill Brothers", "industry": "hvac"})
    general = compose_nurture_draft({"name": "Casey Lin", "company": "Lin Holdings"})
    assert nurture_body_paragraphs(roof.body)[2].rstrip(".").lower() == CASE_STUDIES["roofing"].rstrip(".").lower()
    assert nurture_body_paragraphs(hvac.body)[2].rstrip(".").lower() == TRADES_PROOF.rstrip(".").lower()
    assert nurture_body_paragraphs(general.body)[2].rstrip(".").lower() == GENERAL_PROOF.rstrip(".").lower()
    assert "$2M" not in roof.body
    assert "14+" not in roof.body


def test_g7_rejects_missing_ps_and_keeps_other_gates():
    body = (
        "Hey Pat, we do done-for-you outbound that books qualified meetings for Acme.\n\n"
        f"{THINKING_QUESTIONS['roofing']}\n\n"
        f"{GENERAL_PROOF}\n\n"
        f"{MEETING_GUARANTEE}\n\n"
        "Josh Osborn"
    )
    missing = validate_draft(NurtureDraft(subject="Acme follow up", body=body), {"name": "Pat Reyes"})
    assert missing.valid is False
    assert missing.reject_reason == "no_ps"

    invented = validate_draft(
        NurtureDraft(
            subject="George follow up",
            body=(
                "Hey George, circling back on 47 meetings.\n\n"
                f"{GENERAL_PROOF}\n\n"
                f"{MEETING_GUARANTEE}\n\n"
                "Josh Osborn\n\n"
                f"{PS_TIMING}"
            ),
        ),
        {"name": "George Stradiota", "meeting_at": "2026-04-30T17:00:00+00:00"},
    )
    assert invented.valid is False
    assert invented.reject_reason == "invented_number"

    poc = validate_draft(
        NurtureDraft(subject="Lee follow up", body="Hey Lee, happy to run a free POC.\n\nJosh Osborn\n\nPS: later."),
        {"name": "Lee Ng"},
    )
    assert poc.valid is False
    assert poc.reject_reason == "free_poc"


def test_airpods_flag_never_adds_gift_and_ticker_matches_composer():
    on = compose_nurture_draft(
        {"name": "Dana Ortiz", "industry": "hvac", "last_touch_snippet": "check back in the fall"},
        airpods=True,
    )
    subject, body = draft_email(
        "Dana Ortiz",
        "The Chill Brothers",
        extras={"industry": "hvac", "last_touch_snippet": "check back in the fall"},
    )
    assert "AirPods" not in on.body
    assert "AirPods" not in body
    assert "Worth a look?" not in on.body
    assert "Worth a look?" not in body
    assert body.startswith("Hey Dana, we do done-for-you outbound")
    assert MEETING_GUARANTEE in body
    assert "Josh Osborn" in body
    assert "PS:" in body
    assert subject == "The Chill Brothers follow up"
    assert 90 <= len(_main_words(body)) <= MAX_BODY_WORDS


def test_card_preview_includes_offer_question_and_ps():
    row = {
        "id": "t-preview",
        "name": "Mike Dolan",
        "email": "mike@roofrivercity.test",
        "company": "Roof River City",
        "industry": "roofing",
        "reason": "met",
        "met": True,
        "meeting_at": "2026-04-02T17:00:00+00:00",
        "hs_deal_id": "351000111",
    }
    draft = compose_nurture_draft(row)
    card = build_nurture_card(row, draft)
    preview = card["blocks"][3]["text"]["text"]
    assert preview.startswith("*Email that will send*")
    assert draft.body in preview
    assert "done-for-you outbound" in preview
    assert "PS:" in preview
    assert MEETING_GUARANTEE in preview
    assert "Josh Osborn" in preview
    assert "AirPods" not in preview


def test_nurture_preview_cli_is_read_only(monkeypatch, capsys):
    ticker_id, rest = _parse_nurture_preview(["cycle", "--nurture-preview", "t-mike"])
    assert ticker_id == "t-mike"
    assert rest == ["cycle"]
    missing, _ = _parse_nurture_preview(["--nurture-preview"])
    assert missing == ""

    row = {
        "id": "t-mike",
        "name": "Mike Dolan",
        "email": "mike@roofrivercity.test",
        "company": "Roof River City",
        "industry": "roofing",
        "reason": "met",
        "meeting_at": "2026-04-02T17:00:00+00:00",
    }
    posted = []

    class _Mem:
        def __init__(self, settings):
            del settings

        def get_ticker(self, tid):
            return row if tid == "t-mike" else None

        def patch_ticker(self, *args, **kwargs):
            posted.append(("patch", args, kwargs))

    monkeypatch.setattr("crmbrain.memory.Memory", _Mem)
    settings = make_settings()
    assert _run_nurture_preview(settings, "t-mike") == 0
    out = capsys.readouterr().out
    assert "Subject: Roof River City follow up" in out
    assert "done-for-you outbound" in out
    assert "PS:" in out
    assert posted == []
    assert _run_nurture_preview(settings, "missing") == 2
    assert "ticker not found" in capsys.readouterr().out
