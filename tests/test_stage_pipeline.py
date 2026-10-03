"""Oct 2026 HubSpot pipeline: IDs, no-show counter, renewals, write refusals."""

from datetime import datetime, timedelta, timezone

from crmbrain.config import (
    DELETED_STAGE_IDS,
    NO_SHOW_HINT,
    RENEWAL_PIPELINE,
    RENEWAL_STAGE,
    STAGE,
    canonicalize_stage,
    is_deleted_stage,
)
from crmbrain.documents import stage_from_signature_mail
from crmbrain.hubspot import increment_no_show_count
from crmbrain.models import Engagement
from crmbrain.cycle import apply_gmail_stage_update
from crmbrain.policy import (
    CLOSED_WON_STAGES,
    INCREMENT_NO_SHOW,
    choose_deal_action,
    closed_won_deals,
    is_newer_negative_evidence,
    live_open_deals,
    may_open_new_deal,
    needs_stakeholder_approval,
    resolve_stage,
    should_move_stage,
    unclear_nurture,
)
from crmbrain.renewals import (
    apply_positive_replies,
    at_risk_needed,
    due_for_renewal_create,
    has_existing_client_deal,
    is_evidenced_renewal_call,
    is_renewal_meeting,
    maybe_schedule_renewal_call,
)
from crmbrain.sources.gmail_scan import _stage_from_mail
from tests.test_crm_gating import FakeHubSpot, make_settings


def test_stage_ids_and_aliases():
    assert STAGE["initial_interest"] == "appointmentscheduled"
    assert STAGE["meeting_booked"] == STAGE["discovery_scheduled"] == "qualifiedtobuy"
    assert STAGE["discovery_held"] == STAGE["discovery_completed"] == "presentationscheduled"
    assert STAGE["signed"] == STAGE["contract_signed_unpaid"] == "4391699184"
    assert STAGE["paid"] == STAGE["closed_won"] == "closedwon"
    assert STAGE["needs_stakeholder_approval"] == "4391745240"
    assert STAGE["poc"] == "4391745241"
    assert "no_show" not in STAGE
    assert "3482933986" not in STAGE.values()
    assert "3557889773" not in STAGE.values()
    assert canonicalize_stage("3482933986") == "closedwon"
    assert canonicalize_stage("3557889773") == "qualifiedtobuy"
    assert is_deleted_stage("3482933986")
    assert is_deleted_stage("3557889773")
    assert CLOSED_WON_STAGES == {STAGE["closed_won"]}
    assert STAGE["signed"] not in CLOSED_WON_STAGES


def test_renewal_ids():
    assert RENEWAL_PIPELINE == "2604181234"
    assert RENEWAL_STAGE["renewal_upcoming"] == "4391699185"
    assert RENEWAL_STAGE["call_scheduled"] == "4391699186"
    assert RENEWAL_STAGE["at_risk"] == "4391699187"
    assert RENEWAL_STAGE["renewed"] == "4392753853"
    assert RENEWAL_STAGE["churned"] == "4392753854"


def test_esign_is_contract_unpaid_payment_is_closed_won():
    assert stage_from_signature_mail("Document completed", "PandaDoc", "has been signed")[0] == STAGE[
        "contract_signed_unpaid"
    ]
    assert _stage_from_mail("You received a $8500 payment", "HubSpot Payments", "") == STAGE["closed_won"]
    assert _stage_from_mail("Invoice sent", "QuickBooks", "sent you an invoice") == STAGE[
        "contract_signed_unpaid"
    ]
    assert _stage_from_mail("Invitee no-show", "Calendly", "no-show") == NO_SHOW_HINT


def test_never_write_deleted_or_move_won_to_lost():
    ev = Engagement(source="gmail", external_id="x")
    assert choose_deal_action(STAGE["closed_won"], STAGE["closed_lost"], ev) is None
    assert choose_deal_action(STAGE["closed_won"], STAGE["nurture"], ev) is None
    assert choose_deal_action(STAGE["meeting_booked"], "3482933986", ev) is None
    assert choose_deal_action(STAGE["meeting_booked"], "3557889773", ev) is None
    assert choose_deal_action(STAGE["meeting_booked"], NO_SHOW_HINT, ev) is None
    unpaid = Engagement(source="gmail", external_id="y", stage_hint=STAGE["closed_lost"])
    assert choose_deal_action(STAGE["contract_signed_unpaid"], STAGE["closed_lost"], unpaid) is None
    assert choose_deal_action(
        STAGE["contract_signed_unpaid"], STAGE["closed_won"], unpaid
    ) == STAGE["closed_won"]


def test_lateral_proposal_and_stakeholder():
    ev = Engagement(source="fireflies", external_id="ff", transcript="They will present to partners.")
    assert should_move_stage(STAGE["proposal_sent"], STAGE["needs_stakeholder_approval"])
    assert not should_move_stage(STAGE["needs_stakeholder_approval"], STAGE["proposal_sent"])
    assert needs_stakeholder_approval(ev)
    assert resolve_stage(ev) == STAGE["needs_stakeholder_approval"]


def test_nurture_requires_reason():
    ev = Engagement(source="fireflies", external_id="ff", transcript="Discovery with Pat about pipeline.")
    assert resolve_stage(ev, {"stage_hint": "nurture"}) == STAGE["discovery_held"]
    cold = Engagement(source="gmail", external_id="g", summary="maybe later")
    assert resolve_stage(cold, {"stage_hint": "nurture"}) == ""
    assert unclear_nurture(cold, {"stage_hint": "nurture"})
    assert not unclear_nurture(cold, {"stage_hint": "nurture", "nurture_reason": "budget next FY"})
    assert resolve_stage(cold, {"stage_hint": "nurture", "nurture_reason": "budget next FY"}) == STAGE[
        "nurture"
    ]


def test_no_show_increments_and_leaves_stage():
    hs = FakeHubSpot()
    deal = {
        "id": "d1",
        "contact_id": "1",
        "properties": {"dealstage": STAGE["meeting_booked"], "dealname": "Pat"},
    }
    hs.deals.append(deal)
    assert increment_no_show_count(hs, deal) == 1
    assert increment_no_show_count(hs, deal) == 2
    assert deal["properties"]["dealstage"] == STAGE["meeting_booked"]
    assert deal["properties"]["no_show_count"] == "2"
    assert INCREMENT_NO_SHOW not in DELETED_STAGE_IDS


def test_leftover_paid_id_is_closed_won_not_open():
    leftover = [{"id": "old", "properties": {"dealstage": "3482933986"}}]
    assert closed_won_deals(leftover)
    assert live_open_deals(leftover) == []
    assert has_existing_client_deal(leftover)


def test_renewal_helpers():
    now = datetime(2026, 10, 3, tzinfo=timezone.utc)
    won = {
        "id": "w1",
        "properties": {
            "dealstage": STAGE["closed_won"],
            "contract_end_date": (now + timedelta(days=20)).isoformat(),
            "monthly_fee": "8500",
        },
    }
    assert due_for_renewal_create(won, now=now)
    assert at_risk_needed(11)
    assert not at_risk_needed(12)
    ev = Engagement(source="calendly", external_id="c", raw_subject="QBR / renewal review")
    assert is_renewal_meeting(ev)
    hs = FakeHubSpot()
    renewal = {
        "id": "r1",
        "contact_id": "1",
        "properties": {
            "dealstage": RENEWAL_STAGE["renewal_upcoming"],
            "pipeline": RENEWAL_PIPELINE,
            "dealname": "Client - Renewal",
        },
    }
    hs.deals.append(renewal)
    assert apply_positive_replies(hs, renewal, 4) == RENEWAL_STAGE["at_risk"]
    assert renewal["properties"]["dealstage"] == RENEWAL_STAGE["at_risk"]
    assert renewal["properties"]["positive_replies_30d"] == "4"


def test_nsa_does_not_move_back_to_proposal_sent_from_older_evidence():
    """Item 6: Tyler / Earl stay in Needs Stakeholder Approval on 9/18 re-extraction."""
    nsa = STAGE["needs_stakeholder_approval"]
    ps = STAGE["proposal_sent"]
    assert not should_move_stage(nsa, ps)
    tyler = Engagement(
        source="fireflies",
        external_id="ff-tyler-918",
        occurred_at=datetime(2026, 9, 18, 16, 0, tzinfo=timezone.utc),
        email="tyler@deeprootscapital.com",
        first_name="Tyler",
        last_name="Leverington",
        name="Tyler Leverington",
        transcript="Walked pricing again. Same $20k proposal we already sent.",
        extra={"has_sentences": True, "sentence_count": 12},
    )
    tyler_deal = {
        "id": "350556988142",
        "contact_id": "tyler-1",
        "properties": {
            "dealstage": nsa,
            "dealname": "Tyler Leverington - Deep Roots Capital",
            "amount": "20000",
            "hs_lastmodifieddate": "2026-09-20T12:00:00Z",
        },
        "propertiesWithHistory": {
            "dealstage": [{"value": nsa, "timestamp": "2026-09-20T12:00:00Z", "sourceType": "CRM_UI"}]
        },
    }
    assert choose_deal_action(nsa, ps, tyler, deal=tyler_deal) is None
    assert not is_newer_negative_evidence(tyler, tyler_deal)

    earl = Engagement(
        source="gmail",
        external_id="g-earl-intro",
        occurred_at=datetime(2026, 9, 18, 14, 0, tzinfo=timezone.utc),
        email="ej@accg-inc.com",
        first_name="Earl",
        last_name="Jackson",
        name="Earl Jackson",
        raw_subject="Intro to Earl",
        summary="Quick intro email plus the original proposal recap.",
    )
    earl_deal = {
        "id": "351592972993",
        "contact_id": "earl-1",
        "properties": {
            "dealstage": nsa,
            "dealname": "Earl Jackson - Accg",
            "amount": "20000",
            "hs_lastmodifieddate": "2026-09-22T12:00:00Z",
        },
    }
    assert choose_deal_action(nsa, ps, earl, deal=earl_deal) is None
    lost = Engagement(
        source="gmail",
        external_id="g-lost",
        occurred_at=datetime(2026, 10, 2, 15, 0, tzinfo=timezone.utc),
        email="ej@accg-inc.com",
        raw_subject="We are going with someone else",
        summary="Lost the deal. They rejected the quote and asked for a re-quote later.",
        stage_hint="lost",
    )
    assert is_newer_negative_evidence(lost, earl_deal)
    assert should_move_stage(nsa, ps, newer_negative=True)


def test_no_show_count_increment_path(tmp_path):
    """Item 8: no-show increments the counter and leaves Meeting Booked in place."""
    from tests.test_held_beats_noshow import _gmail_no_show, _prep

    hs, memory, report = _prep(tmp_path)
    past = datetime(2026, 9, 29, 11, 30, tzinfo=timezone.utc)
    ev = _gmail_no_show(scheduled_at=past, already_id="g-increment-path")
    apply_gmail_stage_update(ev, make_settings(dry_run=False), hs, memory, None, report, held_events=[])
    deal = hs.deals[0]
    assert deal["properties"]["dealstage"] == STAGE["meeting_booked"]
    assert deal["properties"].get("no_show_count") == "1"
    assert any("no_show_count++" in line for line in report.deals_moved)
    apply_gmail_stage_update(
        _gmail_no_show(scheduled_at=past, already_id="g-increment-path-2"),
        make_settings(dry_run=False),
        hs,
        memory,
        None,
        report,
        held_events=[],
    )
    assert deal["properties"].get("no_show_count") == "2"
    assert deal["properties"]["dealstage"] == STAGE["meeting_booked"]


def test_nsa_backward_guard_holds_on_september_first():
    nsa = STAGE["needs_stakeholder_approval"]
    ps = STAGE["proposal_sent"]
    earl = Engagement(
        source="gmail",
        external_id="g-earl-91",
        occurred_at=datetime(2026, 9, 1, 14, 0, tzinfo=timezone.utc),
        email="ej@accg-inc.com",
        first_name="Earl",
        last_name="Jackson",
        raw_subject="Intro to Earl",
        summary="Quick intro email plus the original proposal recap.",
        stage_hint=ps,
    )
    earl_deal = {
        "id": "351592972993",
        "properties": {
            "dealstage": nsa,
            "dealname": "Earl Jackson - Accg",
            "amount": "20000",
            "hs_lastmodifieddate": "2026-08-20T12:00:00Z",
        },
    }
    assert choose_deal_action(nsa, ps, earl, deal=earl_deal) is None
    assert not should_move_stage(nsa, ps)


def test_nurture_does_not_advance_without_new_meeting_after_enter():
    ev = Engagement(
        source="fireflies",
        external_id="ff-kevin-old",
        occurred_at=datetime(2026, 8, 1, 18, 0, tzinfo=timezone.utc),
        email="kevin@kevinhagemoser.com",
        name="Kevin Hagemoser",
        transcript="Let's knock out a website. I will pick ONE offer.",
        extra={"has_sentences": True, "sentence_count": 8},
        stage_hint=STAGE["proposal_sent"],
    )
    deal = {
        "id": "d-kevin",
        "properties": {
            "dealstage": STAGE["nurture"],
            "dealname": "Kevin Hagemoser",
            "hs_v2_date_entered_current_stage": "2026-08-15T00:00:00Z",
        },
        "propertiesWithHistory": {
            "dealstage": [
                {
                    "value": STAGE["nurture"],
                    "timestamp": "2026-08-15T00:00:00Z",
                    "sourceType": "CRM_UI",
                }
            ]
        },
    }
    assert choose_deal_action(STAGE["nurture"], STAGE["proposal_sent"], ev, deal=deal) is None
    fresh = Engagement(
        source="fireflies",
        external_id="ff-kevin-new",
        occurred_at=datetime(2026, 9, 10, 18, 0, tzinfo=timezone.utc),
        email="kevin@kevinhagemoser.com",
        name="Kevin Hagemoser",
        transcript="Discovery follow-up. Same website plan.",
        extra={"has_sentences": True, "sentence_count": 8},
        stage_hint=STAGE["discovery_held"],
    )
    assert (
        choose_deal_action(STAGE["nurture"], STAGE["discovery_held"], fresh, deal=deal)
        == STAGE["discovery_held"]
    )


def test_travis_discovery_held_requires_held_meeting():
    gmail = Engagement(
        source="gmail",
        external_id="g-travis",
        email="travis@example.com",
        name="Travis L",
        raw_subject="Catching up",
        summary="Can we talk next week?",
        stage_hint=STAGE["discovery_held"],
    )
    assert choose_deal_action(STAGE["meeting_booked"], STAGE["discovery_held"], gmail) is None
    held = Engagement(
        source="fireflies",
        external_id="ff-travis",
        email="travis@example.com",
        name="Travis L",
        transcript="Discovery with Travis about their roofing pipeline and owners.",
        extra={"has_sentences": True, "sentence_count": 10},
        stage_hint=STAGE["discovery_held"],
    )
    assert (
        choose_deal_action(STAGE["meeting_booked"], STAGE["discovery_held"], held)
        == STAGE["discovery_held"]
    )


def test_bare_number_is_not_a_quoted_amount():
    from crmbrain.deal_write import authorize_deal_write
    from crmbrain.intelligence import deal_amount_to_write, quote_states_priced_offer

    ev = Engagement(
        source="fireflies",
        external_id="ff-kevin-amt",
        email="kevin@kevinhagemoser.com",
        transcript="We talked about 83234 and maybe 2500 later.",
        extra={"deal_terms": {"quote": "we talked about 83234", "tcv": "83234"}},
    )
    assert quote_states_priced_offer("we talked about 83234") is False
    assert deal_amount_to_write({"properties": {"dealstage": STAGE["nurture"]}}, "83234", ev=ev) == ""
    stage, amount, reason = authorize_deal_write(
        ev,
        requested_stage=STAGE["proposal_sent"],
        amount="83234",
        contact={"id": "c-k"},
        deal={
            "id": "d-k",
            "properties": {
                "dealstage": STAGE["nurture"],
                "hs_v2_date_entered_current_stage": "2026-08-15T00:00:00Z",
            },
        },
        settings=make_settings(),
    )
    assert amount == ""
    assert stage == ""


def test_deal_holder_veto_does_not_move_stage():
    from crmbrain.intent import apply_deal_holder_veto
    from crmbrain.models import IntentDecision
    from crmbrain.policy import stamp_deal_context

    ev = Engagement(
        source="fireflies",
        external_id="ff-veto",
        email="pat@example.com",
        first_name="Pat",
        last_name="Lee",
        raw_subject="Recruiter intro",
        transcript="Pat is a recruiter talking talent acquisition.",
        extra={"has_sentences": True, "sentence_count": 6},
    )
    stamp_deal_context(
        ev,
        {"id": "c-p", "properties": {"email": "pat@example.com"}},
        [{"id": "d-p", "properties": {"dealstage": STAGE["discovery_scheduled"]}}],
    )
    incoming = IntentDecision(
        verdict="no",
        intent="recruiter",
        confidence=0.9,
        reason="Recruiter meeting",
        stage=STAGE["discovery_held"],
        amount="83234",
    )
    decision = apply_deal_holder_veto(ev, incoming)
    assert decision.intent != "recruiter"
    assert decision.stage == ""
    assert decision.amount == ""
    assert "no stage move" in (decision.reason or "").lower()


def test_bolder_renewal_needs_evidenced_call():
    email = Engagement(
        source="gmail",
        external_id="g-bolder",
        email="mike@boldercyberpartners.com",
        raw_subject="Renewal thoughts",
        summary="Can we talk about the renewal sometime?",
    )
    assert is_renewal_meeting(email)
    assert not is_evidenced_renewal_call(email)
    hs = FakeHubSpot()
    renewal = {
        "id": "r-bolder",
        "contact_id": "1",
        "properties": {
            "dealstage": RENEWAL_STAGE["renewal_upcoming"],
            "pipeline": RENEWAL_PIPELINE,
            "dealname": "Bolder - Renewal",
        },
    }
    hs.deals.append(renewal)
    assert maybe_schedule_renewal_call(hs, [renewal], email) is None
    assert renewal["properties"]["dealstage"] == RENEWAL_STAGE["renewal_upcoming"]
    cal = Engagement(
        source="calendly",
        external_id="cal-bolder",
        email="mike@boldercyberpartners.com",
        raw_subject="QBR / renewal review",
        extra={"event_type": "Client renewal", "scheduled_at": "2026-10-10T16:00:00Z"},
    )
    assert is_evidenced_renewal_call(cal)
    assert maybe_schedule_renewal_call(hs, [renewal], cal) is not None
    assert renewal["properties"]["dealstage"] == RENEWAL_STAGE["call_scheduled"]


def test_intro_and_unquoted_gmail_never_set_amount():
    from crmbrain.cycle import _propose_engagement
    from crmbrain.deal_write import authorize_deal_write, propose_deal_write
    from crmbrain.intelligence import deal_amount_to_write
    from crmbrain.models import CycleReport, IntentDecision

    intro = Engagement(
        source="gmail",
        external_id="g-kevin-intro",
        email="kevin@kevinhagemoser.com",
        name="Kevin Hagemoser",
        raw_subject="SalesGlider Intro",
        summary="Great to meet you on the intro.",
        extra={"deal_terms": {"quote": "Sales evidence 83234", "tcv": "83234"}},
    )
    assert deal_amount_to_write(None, "83234", ev=intro) == ""
    stage, amount, reason = authorize_deal_write(
        intro,
        requested_stage=STAGE["meeting_booked"],
        amount="83234",
        contact={"id": "c-k"},
        deal=None,
        settings=make_settings(),
    )
    assert amount == ""
    report = CycleReport()
    _propose_engagement(
        report,
        intro,
        IntentDecision(
            verdict="yes",
            intent="sales",
            reason="Sales evidence (intro)",
            stage=STAGE["meeting_booked"],
            amount="83234",
        ),
        {"id": "c-k"},
    )
    assert all(not w.get("amount") for w in report.proposed_writes)

    reextract = Engagement(
        source="fireflies",
        external_id="ff-kevin-500",
        email="kevin@kevinhagemoser.com",
        transcript="He mentioned 500 somewhere in passing.",
        extra={"deal_terms": {"quote": "mentioned 500", "tcv": "500"}},
    )
    _, re_amount, _ = authorize_deal_write(
        reextract,
        requested_stage=STAGE["discovery_held"],
        amount="500",
        contact={"id": "c-k"},
        deal={
            "id": "347748772577",
            "properties": {"dealstage": STAGE["nurture"]},
        },
        settings=make_settings(),
    )
    assert re_amount == ""

    gmail_fig = Engagement(
        source="gmail",
        external_id="g-unquoted",
        email="pat@example.com",
        raw_subject="Following up",
        extra={"deal_terms": {"quote": "we could do 500", "tcv": "500"}},
    )
    assert deal_amount_to_write(None, "500", ev=gmail_fig) == ""


def test_travis_discovery_held_dedupes_and_needs_held_source():
    from crmbrain.deal_write import propose_deal_write
    from crmbrain.models import CycleReport

    report = CycleReport()
    deal_id = "347687452347"
    intro = Engagement(
        source="gmail",
        external_id="g-travis-intro",
        email="travis@example.com",
        name="Travis L",
        raw_subject="SalesGlider Intro",
    )
    propose_deal_write(
        report,
        action="update",
        label="Travis L",
        stage=STAGE["discovery_held"],
        deal_id=deal_id,
        reason="Sales evidence (intro)",
        ev=intro,
    )
    propose_deal_write(
        report,
        action="update",
        label="Travis L",
        stage=STAGE["discovery_held"],
        deal_id=deal_id,
        reason="reextract current=qualifiedtobuy/",
    )
    propose_deal_write(
        report,
        action="move",
        label="Travis L",
        stage=STAGE["discovery_held"],
        deal_id=deal_id,
        reason="held_beats_noshow",
    )
    assert report.proposed_writes == []

    held = Engagement(
        source="fireflies",
        external_id="ff-travis-held",
        occurred_at=datetime(2026, 9, 18, 17, 0, tzinfo=timezone.utc),
        email="travis@example.com",
        name="Travis L",
        extra={"has_sentences": True, "sentence_count": 10},
    )
    propose_deal_write(
        report,
        action="update",
        label="Travis L",
        stage=STAGE["discovery_held"],
        deal_id=deal_id,
        reason="reextract",
        ev=held,
    )
    propose_deal_write(
        report,
        action="move",
        label="Travis L",
        stage=STAGE["discovery_held"],
        deal_id=deal_id,
        reason="held_beats_noshow",
        ev=held,
    )
    assert len(report.proposed_writes) == 1
    reason = report.proposed_writes[0]["reason"]
    assert "fireflies" in reason
    assert "ff-travis-held" in reason
    assert "2026-09-18" in reason
