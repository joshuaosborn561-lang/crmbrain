"""Deal value / stage extraction: TCV, silent meetings, Gmail proposal/payment, budgets."""

from datetime import datetime, timezone

from crmbrain.budget import WriteBudget
from crmbrain.config import STAGE
from crmbrain.cycle import apply_gmail_stage_update, should_reextract, _apply_transcript_intelligence
from crmbrain.documents import payer_emails_from_body, payment_amount_from_text
from crmbrain.intelligence import (
    EXTRACT_TEXT_CAP,
    amount_to_write,
    extract,
    heuristic_deal_terms,
    heuristic_extract,
    normalize_amount_hint,
    parse_deal_amount,
    quote_matches_source,
    tcv_from_terms,
)
from crmbrain.memory import Memory
from crmbrain.models import CycleReport, Engagement
from crmbrain.policy import (
    call_supports_proposal_sent,
    choose_deal_action,
    is_meeting_held,
    is_silent_meeting,
    requires_josh_meeting_to_open_deal,
    resolve_stage,
)
from crmbrain.sources.cube_acr import DriveFile, report_cube_day_gaps
from crmbrain.sources.fireflies import _format_sentences, _summary_block
from tests.test_crm_gating import FakeHubSpot, make_settings


EARL_PROPOSAL = """
Hi Earl —

Attached is the SalesGlider proposal. The engagement is $20,000.

Josh
"""

TYLER_SUMMARY = """
OVERVIEW:
Tyler agreed to the $20,000 package. I'll send the proposal this week.

SHORTHAND:
- $20k package
- send proposal

ACTION ITEMS:
- Josh send proposal
"""

LAURA_RANGE = "Looking at $3-4k/mo with a 3-month minimum on the retainer."

VINCENT_TIMES = "The retainer is $4k x 3. We'll start after the proposal."

VECTOR_PAYMENT_BODY = """
You received a payment of $2,875.50 from agancman@vectorenergygroup.com
This is installment 1 of the agreement.
Total contract: $8,500
"""


def test_extract_text_cap_is_gemini_sized():
    assert EXTRACT_TEXT_CAP == 200_000


def test_fireflies_passes_overview_shorthand_actions_and_speakers():
    text = _format_sentences(
        [
            {"speaker_name": "Josh Osborn", "text": "The package is $20,000."},
            {"speaker_id": "Tyler", "raw_text": "Send the proposal."},
        ]
    )
    assert "Josh Osborn: The package is $20,000." in text
    assert "Tyler: Send the proposal." in text
    block, bullets, items = _summary_block(
        {
            "overview": "Agreed $20k",
            "shorthand_bullet": ["send proposal"],
            "action_items": ["Josh send proposal"],
        }
    )
    assert "Agreed $20k" in block
    assert "send proposal" in block
    assert bullets == ["send proposal"]
    assert items == ["Josh send proposal"]


def test_earl_proposal_email_is_20000():
    ev = Engagement(
        source="gmail",
        external_id="earl-proposal",
        email="earl@example.com",
        first_name="Earl",
        raw_subject="SalesGlider proposal",
        summary="proposal for Earl",
        transcript=EARL_PROPOSAL,
        extra={"josh_sent_proposal": True, "amount_source": "proposal_email"},
    )
    facts = extract(make_settings(), ev)
    assert facts["amount_hint"] == "20000"
    assert facts["deal_amount"] == "20000"
    assert tcv_from_terms(facts.get("deal_terms") or {}) == "20000"


def test_tyler_fireflies_summary_is_20000():
    ev = Engagement(
        source="fireflies",
        external_id="ff-tyler",
        email="tyler@deeprootscapital.com",
        first_name="Tyler",
        last_name="Leverington",
        summary=TYLER_SUMMARY,
        transcript="Josh Osborn: The package is $20,000.\nTyler: Send the proposal.",
        raw_subject="Tyler Leverington POC",
        extra={
            "overview": "Tyler agreed to the $20,000 package. I'll send the proposal this week.",
            "shorthand_bullet": ["$20k package", "send proposal"],
            "action_items": ["Josh send proposal"],
            "has_sentences": True,
            "sentence_count": 2,
        },
    )
    facts = extract(make_settings(), ev)
    assert facts["amount_hint"] == "20000"
    assert resolve_stage(ev, facts) == STAGE["proposal_sent"]
    assert call_supports_proposal_sent(ev, facts)


def test_laura_range_times_min_term_is_9000():
    assert parse_deal_amount(LAURA_RANGE) == "9000"
    terms = heuristic_deal_terms(LAURA_RANGE)
    assert terms["range_low"] == "3000"
    assert terms["term_months"] == "3"
    assert tcv_from_terms(terms) == "9000"
    facts = heuristic_extract(LAURA_RANGE)
    assert facts["deal_amount"] == "9000"


def test_vincent_4k_x_3_is_12000():
    assert parse_deal_amount(VINCENT_TIMES) == "12000"
    terms = heuristic_deal_terms(VINCENT_TIMES)
    assert terms["monthly_fee"] == "4000"
    assert terms["term_months"] == "3"
    assert tcv_from_terms(terms) == "12000"


def test_price_next_to_meeting_guarantee_is_valid():
    text = "I'll guarantee meetings. The fee is $20,000 for the engagement."
    assert parse_deal_amount(text) == "20000"


def test_quote_fuzzy_match_accepts_model_tcv():
    source = "Looking at $3-4k/mo with a 3-month minimum."
    assert quote_matches_source("$3-4k/mo with a 3-month minimum", source)
    assert normalize_amount_hint("9000", source, quote="$3-4k/mo, 3-month minimum") == "9000"
    assert normalize_amount_hint("4500", "Great discovery. Case study $2M pipeline.") == ""


def test_silent_fireflies_meeting_does_not_move_stage(tmp_path):
    ev = Engagement(
        source="fireflies",
        external_id="ff-silent",
        email="quiet@example.com",
        first_name="Quiet",
        last_name="Lead",
        raw_subject="Quiet Lead and Joshua Osborn",
        extra={"silent_meeting": True, "sentence_count": 0, "has_sentences": False, "summary_status": "silent"},
    )
    assert is_silent_meeting(ev)
    assert not is_meeting_held(ev)
    assert resolve_stage(ev) == ""
    assert resolve_stage(ev, {"stage_hint": "discovery_completed"}) == ""
    assert choose_deal_action(STAGE["nurture"], STAGE["discovery_completed"], ev) is None
    hs = FakeHubSpot(
        [
            {
                "id": "q1",
                "properties": {"email": "quiet@example.com", "firstname": "Quiet", "lastname": "Lead"},
            }
        ]
    )
    hs.deals.append(
        {
            "id": "d-q",
            "contact_id": "q1",
            "properties": {"dealstage": STAGE["nurture"], "dealname": "Quiet", "amount": ""},
        }
    )
    settings = make_settings()
    memory = Memory(settings, data_dir=tmp_path)
    report = CycleReport()
    _apply_transcript_intelligence(ev, settings, hs, memory, report, hs.contacts[0], add_timeline_note=False)
    assert hs.deals[0]["properties"]["dealstage"] == STAGE["nurture"]
    assert not report.deals_moved


def test_nurture_stays_without_new_held_call():
    silent = Engagement(
        source="fireflies",
        external_id="ff-n",
        extra={"silent_meeting": True, "has_sentences": False},
    )
    held = Engagement(
        source="fireflies",
        external_id="ff-h",
        transcript="We walked discovery.",
        extra={"has_sentences": True, "sentence_count": 4},
    )
    assert choose_deal_action(STAGE["nurture"], STAGE["discovery_completed"], silent) is None
    assert choose_deal_action(STAGE["closed_lost"], STAGE["discovery_completed"], silent) is None
    assert choose_deal_action(STAGE["nurture"], STAGE["discovery_completed"], held) == STAGE["discovery_completed"]


def test_vector_payment_body_sets_paid_without_overwriting_total(tmp_path):
    emails = payer_emails_from_body(VECTOR_PAYMENT_BODY)
    assert any(e.startswith("agancman@") for e in emails)
    amount, instalment = payment_amount_from_text(VECTOR_PAYMENT_BODY)
    assert amount == "8500"
    assert instalment is False
    ev = Engagement(
        source="gmail",
        external_id="pay-vector",
        email="agancman@vectorenergygroup.com",
        first_name="Alvaro",
        last_name="Gancman",
        company="Vector Energy Group",
        raw_subject="You received a payment",
        summary="payment received",
        transcript=VECTOR_PAYMENT_BODY,
        stage_hint=STAGE["paid"],
        extra={
            "amount": "2875.50",
            "amount_source": "payment",
            "amount_is_instalment": True,
            "payment": True,
            "hubspot_contact_id": "v1",
        },
    )
    hs = FakeHubSpot(
        [
            {
                "id": "v1",
                "properties": {
                    "email": "agancman@vectorenergygroup.com",
                    "firstname": "Alvaro",
                    "lastname": "Gancman",
                    "company": "Vector Energy Group",
                },
            }
        ]
    )
    hs.deals.append(
        {
            "id": "d-v",
            "contact_id": "v1",
            "properties": {"dealstage": STAGE["paid"], "dealname": "Vector Energy Group", "amount": "8500"},
        }
    )
    settings = make_settings()
    report = CycleReport()
    apply_gmail_stage_update(ev, settings, hs, Memory(settings, data_dir=tmp_path), None, report)
    assert hs.deals[0]["properties"]["dealstage"] == STAGE["paid"]
    assert hs.deals[0]["properties"]["amount"] == "8500"
    assert report.amounts_set == []


def test_hand_corrected_open_amounts_are_not_overwritten_by_calls():
    assert amount_to_write("21000", "18000", stage=STAGE["proposal_sent"], incoming_source="call") == ""
    assert amount_to_write("20000", "20000", stage=STAGE["proposal_sent"], incoming_source="call") == ""
    assert amount_to_write("8500", "2875.50", stage=STAGE["paid"], incoming_source="payment") == ""
    assert amount_to_write("8500", "3000", stage=STAGE["paid"], incoming_source="call") == ""


def test_proposal_email_can_overwrite_call_amount_on_open_stage():
    assert (
        amount_to_write("3000", "20000", stage=STAGE["discovery_completed"], incoming_source="proposal_email")
        == "20000"
    )


def test_amount_budget_is_separate_from_stage(tmp_path):
    budget = WriteBudget(max_creates=0, max_stage_moves=0, max_amount_writes=1)
    assert budget.allow("amount")
    assert not budget.allow("amount")
    assert budget.remaining("amount") == 0
    ev = Engagement(
        source="fireflies",
        external_id="ff-cap",
        email="cap@example.com",
        first_name="Cap",
        transcript="Quoted them $4,500 one-time package.",
        extra={"has_sentences": True, "sentence_count": 1},
    )
    hs = FakeHubSpot(
        [{"id": "c1", "properties": {"email": "cap@example.com", "firstname": "Cap"}}]
    )
    hs.deals.append(
        {
            "id": "d-c",
            "contact_id": "c1",
            "properties": {"dealstage": STAGE["discovery_completed"], "amount": ""},
        }
    )
    settings = make_settings(max_amount_writes=0)
    report = CycleReport()
    empty = WriteBudget(max_creates=10, max_stage_moves=20, max_amount_writes=0)
    _apply_transcript_intelligence(
        ev, settings, hs, Memory(settings, data_dir=tmp_path), report, hs.contacts[0], add_timeline_note=False, budget=empty
    )
    assert hs.deals[0]["properties"].get("amount") in {"", None}
    assert any("cap" in q.lower() for q in report.review_queue)


def test_gemini_missing_key_logs_warning(caplog):
    import logging

    import crmbrain.intelligence as intel

    intel._GEMINI_KEY_WARNED = False
    ev = Engagement(
        source="fireflies",
        external_id="ff-warn",
        transcript="Monthly retainer is $3,000.",
        extra={"has_sentences": True},
    )
    with caplog.at_level(logging.WARNING, logger="crmbrain.intelligence"):
        extract(make_settings(gemini_key=""), ev)
    assert any("gemini key missing" in r.message.lower() for r in caplog.records)


def test_cube_warns_amr_without_transcript_or_missing_folder():
    warnings: list[str] = []
    files = [
        DriveFile(file_id="a1", name="call.amr", mime_type="audio/amr", folder_date="2026-09-30"),
    ]
    report_cube_day_gaps(files, {"2026-09-30", "2026-10-01"}, {"2026-09-30"}, warnings)
    assert any("amr with no transcript" in w for w in warnings)
    assert any("missing day-folder 2026-10-01" in w for w in warnings)


def test_reextract_since_honors_lookback_and_source():
    settings = make_settings(reextract_since=datetime(2026, 9, 1, tzinfo=timezone.utc))
    old = Engagement(
        source="fireflies",
        external_id="old",
        occurred_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    recent = Engagement(
        source="fireflies",
        external_id="new",
        occurred_at=datetime(2026, 9, 15, tzinfo=timezone.utc),
    )
    gmail = Engagement(
        source="gmail",
        external_id="g",
        occurred_at=datetime(2026, 9, 15, tzinfo=timezone.utc),
    )
    assert not should_reextract(settings, old)
    assert should_reextract(settings, recent)
    assert not should_reextract(settings, gmail)


def test_heyreach_and_client_campaign_need_josh_meeting():
    hey = Engagement(source="heyreach", external_id="hr-1", email="sam@acme.com", first_name="Sam")
    client = Engagement(
        source="smartlead",
        external_id="sl-client",
        email="lead@roof.com",
        extra={"client_campaign": True, "campaign_name": "Peterson HVAC"},
    )
    assert requires_josh_meeting_to_open_deal(hey)
    assert requires_josh_meeting_to_open_deal(client)
    assert choose_deal_action(None, STAGE["discovery_completed"], hey) is None
    assert choose_deal_action(None, STAGE["discovery_completed"], client) is None


def test_earl_gmail_apply_sets_proposal_and_amount(tmp_path):
    ev = Engagement(
        source="gmail",
        external_id="earl-mail",
        email="earl@goliath.com",
        first_name="Earl",
        last_name="Buyer",
        raw_subject="SalesGlider proposal",
        transcript=EARL_PROPOSAL,
        stage_hint=STAGE["proposal_sent"],
        extra={
            "amount": "20000",
            "amount_source": "proposal_email",
            "josh_sent_proposal": True,
            "deal_terms": {"monthly_fee": "", "term_months": "", "tcv": "20000", "quote": "$20,000"},
            "hubspot_contact_id": "e1",
        },
    )
    hs = FakeHubSpot(
        [{"id": "e1", "properties": {"email": "earl@goliath.com", "firstname": "Earl", "lastname": "Buyer"}}]
    )
    hs.deals.append(
        {
            "id": "d-e",
            "contact_id": "e1",
            "properties": {"dealstage": STAGE["discovery_completed"], "dealname": "Earl", "amount": ""},
        }
    )
    settings = make_settings()
    report = CycleReport()
    apply_gmail_stage_update(ev, settings, hs, Memory(settings, data_dir=tmp_path), None, report)
    assert hs.deals[0]["properties"]["dealstage"] == STAGE["proposal_sent"]
    assert hs.deals[0]["properties"]["amount"] == "20000"
    assert any("20000" in a for a in report.amounts_set)
    assert any("20000" in (n[1] or "") and "proposal_email" in (n[1] or "") for n in hs.notes)
