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
from crmbrain.policy import (
    CLOSED_WON_STAGES,
    INCREMENT_NO_SHOW,
    choose_deal_action,
    closed_won_deals,
    live_open_deals,
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
    is_renewal_meeting,
)
from crmbrain.sources.gmail_scan import _stage_from_mail
from tests.test_crm_gating import FakeHubSpot


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
    assert should_move_stage(STAGE["needs_stakeholder_approval"], STAGE["proposal_sent"])
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
