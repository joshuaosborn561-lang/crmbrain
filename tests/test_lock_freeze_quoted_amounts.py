"""Lock/freeze on archive+move, quoted call amounts, and client_ops Paid/Signed only."""

from datetime import datetime, timezone

from crmbrain.config import STAGE
from crmbrain.deal_write import (
    authorize_deal_lifecycle,
    authorize_deal_write,
    commit_deal_archive,
    commit_deal_move,
)
from crmbrain.hubspot import HubSpot
from crmbrain.intelligence import (
    _call_amount_from_gemini,
    heuristic_deal_terms,
    quote_states_priced_offer,
    tcv_from_terms,
)
from crmbrain.intent import classify, normalize_client_ops
from crmbrain.models import CycleReport, Engagement, IntentDecision
from crmbrain.policy import stamp_deal_context
from crmbrain.prune import prune_replied_deals
from tests.test_crm_gating import FakeHubSpot, make_settings

FREEZE = datetime(2026, 10, 3, 1, 30, tzinfo=timezone.utc)
SEP_24 = datetime(2026, 9, 24, 16, 0, tzinfo=timezone.utc)
OCT_4 = datetime(2026, 10, 4, 16, 0, tzinfo=timezone.utc)

TYLER_SEP24_GENERIC = (
    "Most of my clients pay between 3 and 4 grand a month. "
    "We have been doing this for 36 months as a company. "
    "Tyler said he cannot commit yet."
)
TYLER_SEP29_OFFER = "5,000 a month for four months"
EARL_SEP30_OFFER = "The retainer is $3,500 x 6."
VECTOR_PACKAGE = "8500 for three months"


def _deal(did, stage, amount="", locked="", name=""):
    props = {"dealstage": stage, "dealname": name or did, "amount": amount}
    if locked:
        props["crmbrain_locked"] = locked
    return {"id": did, "properties": props}


def _contact(cid="c-dave", email="dave@goliath.com"):
    return {
        "id": cid,
        "properties": {
            "email": email,
            "firstname": "Dave",
            "lastname": "Ackley",
            "company": "Goliath",
        },
    }


def test_locked_deal_never_archived_or_moved():
    settings = make_settings(manual_freeze_at=FREEZE)
    locked = _deal("352237549244", STAGE["replied"], locked="true", name="Locked A")
    other = _deal("352220213963", STAGE["discovery_scheduled"], locked="true", name="Locked B")
    report = CycleReport()
    hs = FakeHubSpot()
    hs.settings = settings
    hs.deals = [locked, other]
    ev = Engagement(source="fireflies", external_id="ff-lock", occurred_at=OCT_4)
    assert authorize_deal_lifecycle(locked, ev=ev, settings=settings, action="archive") == (
        False,
        "locked",
    )
    assert commit_deal_archive(hs, locked, ev=ev, settings=settings, report=report) is False
    assert commit_deal_move(
        hs, other, STAGE["discovery_completed"], ev=ev, settings=settings, report=report
    ) is False
    assert {d["id"] for d in hs.deals} == {"352237549244", "352220213963"}
    assert hs.deals[1]["properties"]["dealstage"] == STAGE["discovery_scheduled"]
    assert any("refused locked" in line for line in report.review_queue)


def test_pre_freeze_event_never_archives():
    settings = make_settings(manual_freeze_at=FREEZE)
    deal = _deal("d-pre", STAGE["replied"], name="Pre-freeze")
    ev = Engagement(source="gmail", external_id="g-pre", occurred_at=SEP_24)
    report = CycleReport()
    hs = FakeHubSpot()
    hs.settings = settings
    hs.deals = [deal]
    assert authorize_deal_lifecycle(deal, ev=ev, settings=settings, action="archive") == (
        False,
        "manual_freeze",
    )
    assert commit_deal_archive(hs, deal, ev=ev, settings=settings, report=report) is False
    assert hs.deals[0]["id"] == "d-pre"
    assert any("manual_freeze" in line for line in report.review_queue)


def test_archive_duplicate_skips_locked_deal():
    settings = make_settings(manual_freeze_at=FREEZE)
    keep = _deal("keep-1", STAGE["discovery_scheduled"], name="Keep This Richer Deal")
    locked = _deal("352237549244", STAGE["discovery_scheduled"], locked="true", name="Dup")
    hs = type("HS", (), {})()
    hs.settings = settings
    hs.report = CycleReport()
    hs.archived = []

    def archive_deal(deal_id):
        hs.archived.append(deal_id)

    hs.archive_deal = archive_deal
    leftover = HubSpot._archive_duplicate_deals(hs, [keep, locked])
    assert "352237549244" not in hs.archived
    assert any(d["id"] == "352237549244" for d in leftover)
    assert any("locked" in line for line in hs.report.review_queue)


def test_prune_does_not_archive_or_move_locked_replied():
    settings = make_settings()
    hs = FakeHubSpot(
        [{"id": "c1", "properties": {"email": "a@b.com", "firstname": "Ann", "crm_source": ""}}]
    )
    hs.settings = settings
    hs.deals.append(
        {
            "id": "352220213963",
            "contact_id": "c1",
            "properties": {
                "dealstage": STAGE["replied"],
                "dealname": "Locked Replied",
                "crmbrain_locked": "true",
            },
        }
    )
    report = CycleReport()
    prune_replied_deals(hs, report)
    assert hs.deals[0]["id"] == "352220213963"
    assert hs.deals[0]["properties"]["dealstage"] == STAGE["replied"]
    assert not any(w[0] == "archive_deal" for w in hs.writes)
    assert not any(w[0] == "move_deal" for w in hs.writes)
    assert any("locked" in line for line in report.review_queue)


def test_tyler_sep24_generic_range_yields_no_amount():
    quote = "most of my clients pay between 3 and 4 grand a month"
    terms = {
        "monthly_fee": "3000",
        "term_months": "36",
        "tcv": "144000",
        "quote": quote,
    }
    assert quote_states_priced_offer(quote) is False
    assert tcv_from_terms(terms) == ""
    assert _call_amount_from_gemini({"deal_terms": terms}, TYLER_SEP24_GENERIC) == ""
    heur = heuristic_deal_terms(TYLER_SEP24_GENERIC)
    assert tcv_from_terms(heur) != "144000"
    assert heur.get("tcv") != "144000"


def test_quoted_term_positive_tyler_earl_vector():
    tyler = heuristic_deal_terms(TYLER_SEP29_OFFER)
    assert tcv_from_terms(tyler) == "20000"
    assert _call_amount_from_gemini(
        {
            "deal_terms": {
                "monthly_fee": "5000",
                "term_months": "4",
                "tcv": "20000",
                "quote": TYLER_SEP29_OFFER,
            }
        },
        f"Tyler agreed to {TYLER_SEP29_OFFER}.",
    ) == "20000"

    earl = heuristic_deal_terms(EARL_SEP30_OFFER)
    assert tcv_from_terms(earl) == "21000"
    assert _call_amount_from_gemini(
        {
            "deal_terms": {
                "monthly_fee": "3500",
                "term_months": "6",
                "tcv": "21000",
                "quote": "$3,500 x 6",
            }
        },
        EARL_SEP30_OFFER,
    ) == "21000"

    vector = heuristic_deal_terms(VECTOR_PACKAGE)
    assert tcv_from_terms(vector) == "8500"
    assert _call_amount_from_gemini(
        {
            "deal_terms": {
                "monthly_fee": "8500",
                "term_months": "3",
                "tcv": "8500",
                "quote": VECTOR_PACKAGE,
            }
        },
        f"Vector paid {VECTOR_PACKAGE}.",
    ) == "8500"


def test_dave_cube_open_proposal_sent_is_not_client_ops():
    ev = Engagement(
        source="cube_acr",
        external_id="cube-dave",
        occurred_at=OCT_4,
        email="dave@goliath.com",
        first_name="Dave",
        last_name="Ackley",
        company="Goliath",
        raw_subject="Meraki Discussion with Insight",
        transcript=(
            "Talked about insight.com meraki and the Goliath campaign ops. "
            "No new paperwork. Just a routine check-in on the existing proposal."
        ),
        extra={"has_sentences": True, "sentence_count": 8},
    )
    contact = _contact()
    deal = {
        "id": "340447563471",
        "contact_id": "c-dave",
        "properties": {
            "dealstage": STAGE["proposal_sent"],
            "dealname": "Dave Ackley - Goliath",
            "amount": "21000",
        },
    }
    stamp_deal_context(ev, contact, [deal])
    gemini_ops = IntentDecision(
        verdict="no",
        intent="client_ops",
        confidence=0.88,
        reason="Gemini client ops",
        via="gemini",
    )
    rewritten = normalize_client_ops(ev, gemini_ops)
    assert rewritten.intent != "client_ops"
    assert rewritten.verdict != "no"
    classified = classify(make_settings(), ev)
    assert classified.intent != "client_ops"
    assert classified.verdict != "no"

    settings = make_settings(manual_freeze_at=FREEZE)
    stage, amount, reason = authorize_deal_write(
        ev,
        requested_stage=STAGE["discovery_completed"],
        contact=contact,
        deal=deal,
        settings=settings,
    )
    assert reason != "closed_won"
    assert reason != "not_deal"
    # Open PS may refresh after freeze; a pre-freeze Cube call may not.
    assert reason in {"refresh", "no_write", "move"}
    early = Engagement(
        source="cube_acr",
        external_id="cube-dave-early",
        occurred_at=SEP_24,
        email="dave@goliath.com",
        first_name="Dave",
        last_name="Ackley",
        company="Goliath",
        transcript=ev.transcript,
    )
    stamp_deal_context(early, contact, [deal])
    _stage, _amt, frozen = authorize_deal_write(
        early,
        requested_stage=STAGE["discovery_completed"],
        contact=contact,
        deal=deal,
        settings=settings,
    )
    assert frozen == "frozen"
