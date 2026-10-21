from types import SimpleNamespace
from crmbrain import cycle, policy
from crmbrain.config import STAGE
from crmbrain.models import Engagement

MB = STAGE["meeting_booked"]

def _ev(title, email="x@acme.com", intent=None):
    ev = Engagement(source="calendly", external_id="gcal:1", email=email, raw_subject=title, stage_hint=MB, extra={"event_type": title})
    if intent:
        ev._person_intent = SimpleNamespace(intent=intent, confidence=0.9)
    return ev

def _deal(stage):
    return {"id": "1", "properties": {"dealstage": stage}}

class FakeHS:
    def __init__(self, deals):
        self.deals = deals
    def find_contact(self, email="", phone="", name=""):
        return {"id": "c1"}
    def open_deals_for_contact(self, cid):
        return self.deals

def test_no_deal_bookings_do_not_count():
    for title, intent in [("Podcast Carl Willis", None), ("Lunch: Miller / Josh", None), ("Mark/Josh Osborn", None), ("Cynthia/Josh Monthly", "networking"), ("Kyle check-in", "client_ops")]:
        assert not cycle.calendar_booking_would_write(_ev(title, intent=intent), FakeHS([]))

def test_new_prospect_counts():
    assert cycle.calendar_booking_would_write(_ev("Gabriel Lopez and Joshua Osborn"), FakeHS([]))

def test_same_or_later_stage_does_not_count():
    for stage in [MB, STAGE["discovery_held"], STAGE["proposal_sent"], STAGE["needs_stakeholder_approval"], STAGE["contract_signed_unpaid"], STAGE["closed_won"], "4392753853", "4392753854"]:
        assert not cycle.calendar_booking_would_write(_ev("Sales call"), FakeHS([_deal(stage)])), stage

def test_never_move_back_to_meeting_booked():
    for stage in [STAGE["discovery_held"], STAGE["proposal_sent"], STAGE["needs_stakeholder_approval"], STAGE["contract_signed_unpaid"], STAGE["closed_won"], "4392753853", "4392753854"]:
        assert not policy.should_move_stage(stage, MB), stage
    assert policy.should_move_stage(STAGE["initial_interest"], MB)
