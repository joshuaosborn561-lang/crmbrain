"""Dry-run #63 follow-ups: learning/vendor, zoom rooms, money-stage floors, commerce match."""

from datetime import datetime, timezone
from dataclasses import replace

from crmbrain.calendar_events import is_excluded_attendee
from crmbrain.config import CDT, STAGE, compute_lookback_start, is_zoom_room_address
from crmbrain.cycle import _queue_linkedin, apply_gmail_stage_update
from crmbrain.documents import commerce_match_fields
from crmbrain.intent import heuristic_intent
from crmbrain.memory import Memory
from crmbrain.models import CycleReport, Engagement
from crmbrain.policy import choose_deal_action
from crmbrain.sources.gmail_scan import is_system_address, scan, scan_people
from crmbrain.ticker import TickerCandidate, enroll, plan_enrollments
from tests.test_crm_gating import FakeHubSpot, make_settings


def test_learning_and_vendor_keywords_do_not_tag_prospects():
    tyler = Engagement(
        source="fireflies",
        external_id="ff-tyler",
        email="tyler@deeprootscapital.com",
        first_name="Tyler",
        last_name="Leverington",
        raw_subject="Tyler Leverington POC",
        transcript="How do you generate leads? They want to learn about a paid POC.",
    )
    alvaro = Engagement(
        source="fireflies",
        external_id="ff-alvaro",
        email="alvaro@vectorenergy.com",
        first_name="Alvaro",
        last_name="Gancman",
        company="Vector Energy",
        raw_subject="Vector Energy discovery",
        transcript="Alvaro asked how do you run outbound. Learn about SalesGlider pricing.",
    )
    cube = Engagement(
        source="cube_acr",
        external_id="cube-randy",
        first_name="Randy",
        last_name="Haba",
        phone="15551212",
        transcript="Randy said their vendor is not booking meetings. Intro on the HVAC list.",
        raw_subject="Randy Haba Cube",
    )
    for ev in (tyler, alvaro, cube):
        decision = heuristic_intent(ev)
        assert decision.intent not in {"learning", "vendor"}, (ev.email or ev.name, decision)

    master = Engagement(
        source="calendly",
        external_id="chorbie",
        email="cynthia@chorbie.com",
        name="Cynthia Hernandez",
        raw_subject="Marketing Masterclass",
        extra={"event_type": "Marketing Masterclass"},
    )
    assert heuristic_intent(master).intent == "learning"


def test_zoom_room_addresses_are_not_identities():
    room = "82582065195@zoomcrc.com"
    assert is_zoom_room_address(room)
    assert is_system_address(room)
    assert is_excluded_attendee(room)
    hs = FakeHubSpot(
        [
            {
                "id": "c-dave",
                "properties": {
                    "email": "dave@goliath.com",
                    "firstname": "Dave",
                    "lastname": "Ackley",
                    "company": "Goliath",
                },
            }
        ]
    )
    assert hs.find_contact(email=room) is None
    assert hs.find_contact(email=room, name="Dave Ackley") is None


def test_gmail_never_moves_signed_or_proposal_backward(tmp_path):
    settings = make_settings(dry_run=True)
    contact = {
        "id": "c-dave",
        "properties": {
            "email": "dave@goliath.com",
            "firstname": "Dave",
            "lastname": "Ackley",
            "company": "Goliath",
        },
    }
    hs = FakeHubSpot([contact])
    hs.deals.append(
        {
            "id": "d-dave",
            "contact_id": "c-dave",
            "properties": {
                "dealstage": STAGE["signed"],
                "dealname": "Dave Ackley - Goliath",
                "amount": "21000",
            },
        }
    )
    ev = Engagement(
        source="gmail",
        external_id="gcal-dave",
        email="dave@goliath.com",
        first_name="Dave",
        last_name="Ackley",
        raw_subject="Invitation: Dave Ackley",
        stage_hint=STAGE["discovery_scheduled"],
    )
    report = CycleReport()
    apply_gmail_stage_update(ev, settings, hs, Memory(settings, data_dir=tmp_path), None, report)
    assert hs.deals[0]["properties"]["dealstage"] == STAGE["signed"]
    assert not any(
        (w.get("stage") == STAGE["discovery_scheduled"]) for w in report.proposed_writes
    )
    assert choose_deal_action(STAGE["signed"], STAGE["discovery_scheduled"], ev) is None
    assert choose_deal_action(STAGE["paid"], STAGE["discovery_scheduled"], ev) is None
    assert choose_deal_action(STAGE["proposal_sent"], STAGE["discovery_scheduled"], ev) is None


def test_payment_and_agreement_mail_match_by_name_company_amount():
    payer, company, amount = commerce_match_fields(
        "You received a payment of $2,875.50 from Dave Ackley",
        "You received a payment of $2,875.50 from Dave Ackley",
    )
    assert payer.lower().startswith("dave")
    assert amount in {"2875.5", "2875.50", "2876"}
    _, company, _ = commerce_match_fields(
        "Updated agreement: Vector Energy Group x SalesGlider",
        "PandaDoc updated the agreement",
    )
    assert "vector energy" in company.lower()

    contact = {
        "id": "c-alvaro",
        "properties": {
            "email": "alvaro@vectorenergy.com",
            "firstname": "Alvaro",
            "lastname": "Gancman",
            "company": "Vector Energy Group",
        },
    }
    hs = FakeHubSpot([contact])
    hs.deals.append(
        {
            "id": "d-alvaro",
            "contact_id": "c-alvaro",
            "properties": {"dealstage": STAGE["proposal_sent"], "amount": "2875.50"},
        }
    )
    assert hs.find_contact_for_commerce(company="Vector Energy Group") is None
    assert hs.find_contact_for_commerce(amount="2875.50") is None
    assert hs.find_contact_for_commerce(name="Alvaro Gancman").get("id") == "c-alvaro"
    assert hs.find_contact_for_commerce(email="alvaro@vectorenergy.com").get("id") == "c-alvaro"

    class PayGmail:
        def search(self, query, max_results=30):
            return [{"id": "pay-1"}]

        def get(self, mid):
            return {
                "id": mid,
                "internalDate": "1728000000000",
                "snippet": "You received a payment of $2,875.50 from Alvaro Gancman",
                "_headers": {
                    "from": "Stripe <noreply@stripe.com>",
                    "to": "Joshua <joshua@salesglidergrowth.com>",
                    "subject": "You received a payment of $2,875.50",
                },
                "_body": "You received a payment of $2,875.50 from Alvaro Gancman",
            }

        def headers_map(self, msg):
            return msg["_headers"]

        def body_text(self, msg):
            return msg["_body"]

        def calendar_parts(self, msg):
            return []

    settings = make_settings()
    report = CycleReport()
    events = scan(settings, PayGmail(), hs, report)
    assert events
    assert events[0].email == "alvaro@vectorenergy.com"
    assert events[0].stage_hint == STAGE["paid"]
    assert not any("not in CRM" in x for x in report.junk_blocked)


def test_gmail_people_skips_one_403_and_continues():
    class PartialGmail:
        def search(self, query, max_results=80):
            return [{"id": "ok-1"}, {"id": "bad-403"}]

        def get(self, mid):
            if mid == "bad-403":
                raise RuntimeError("403 Forbidden")
            return {
                "id": mid,
                "internalDate": "1728000000000",
                "snippet": "hello",
                "_headers": {
                    "from": "Pat Lee <pat@clientco.com>",
                    "to": "Joshua <joshua@salesglidergrowth.com>",
                    "subject": "intro",
                },
            }

        def headers_map(self, msg):
            return msg["_headers"]

    settings = make_settings()
    report = CycleReport()
    people = scan_people(settings, PartialGmail(), report=report)
    assert any(ev.email == "pat@clientco.com" for ev in people)
    assert any("bad-403" in w for w in report.warnings)
    assert any("bad-403" in s for s in report.skipped)


def test_lookback_start_override_is_not_capped_and_keeps_write_caps():
    start = datetime(2026, 9, 18, tzinfo=CDT)
    settings = make_settings(
        lookback_start_at=start,
        lookback_override=True,
        max_creates=10,
        max_stage_moves=20,
    )
    ancient = datetime(2026, 1, 1, tzinfo=timezone.utc)
    now = datetime(2026, 10, 2, 18, 0, tzinfo=timezone.utc)
    got = compute_lookback_start(settings, ancient, now=now)
    assert got.astimezone(CDT).date().isoformat() == "2026-09-18"
    assert settings.max_creates == 10
    assert settings.max_stage_moves == 20
    capped = compute_lookback_start(replace(settings, lookback_override=False), ancient, now=now)
    assert capped > start


def test_heyreach_skips_booked_deal_and_ticker_rejects_nameless(tmp_path):
    settings = make_settings()
    memory = Memory(settings, data_dir=tmp_path)
    report = CycleReport()
    contact = {
        "id": "c-boyd",
        "properties": {
            "email": "jboyd@boydsoftx.com",
            "firstname": "John",
            "lastname": "Boyd",
        },
    }
    hs = FakeHubSpot([contact])
    hs.scheduled_attendee_emails = {"jboyd@boydsoftx.com"}
    hs.deals.append(
        {
            "id": "d-boyd",
            "contact_id": "c-boyd",
            "properties": {"dealstage": STAGE["discovery_scheduled"], "dealname": "John Boyd"},
        }
    )

    class FakeHey:
        def __init__(self):
            self.added = []

        def add_lead(self, ev):
            self.added.append(ev)
            return "queued"

    hey = FakeHey()
    booked = Engagement(
        source="calendly",
        external_id="cal-boyd",
        email="jboyd@boydsoftx.com",
        first_name="John",
        last_name="Boyd",
        linkedin_url="https://www.linkedin.com/in/johnboyd",
        extra={"create_new": True, "event_type": "SalesGlider Intro"},
    )
    _queue_linkedin(settings, hey, booked, hs, memory, report, contact=contact)
    assert hey.added == []
    assert any("meeting_or_deal" in s for s in report.skipped)

    nameless = Engagement(source="smartlead", external_id="sl-x", email="noname@x.com")
    assert enroll(memory, nameless, "never_booked") == {}
    rows, skipped = plan_enrollments(
        [TickerCandidate(name="", email="noname@x.com", reason="never_booked")],
        [],
    )
    assert rows == []
    assert skipped and skipped[0].skip_reason == "no_name"
