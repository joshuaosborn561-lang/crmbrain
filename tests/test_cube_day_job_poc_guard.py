"""Cube day_job / bare POC must not open HubSpot contacts or deals."""

from datetime import datetime, timezone

from crmbrain.config import STAGE
from crmbrain.cycle import _handle_engagement
from crmbrain.evidence import KIND_POC, build_timelines, kind_for
from crmbrain.intent import (
    KNOWN_NON_SALES_PEOPLE,
    KNOWN_NON_SALES_PHONES,
    attach_person_intent,
    heuristic_intent,
    is_confident_non_sales,
    is_confident_sales,
)
from crmbrain.memory import Memory
from crmbrain.models import CycleReport, Engagement, IntentDecision
from crmbrain.policy import (
    cube_has_sales_intent,
    has_salesglider_offer_talk,
    held_call_may_open_deal,
    is_cube_business_discovery,
    may_create_hubspot_contact,
    resolve_stage,
    stamp_deal_context,
)
from crmbrain.reconcile import apply_timeline, restore_missing_deals
from tests.test_crm_gating import FakeHubSpot, make_settings


CAMERON_PHONE = "+12145466567"
# Wed Oct 7 2026 12:14pm CT Cube ACR with Cameron Hawkins (QA archived HS 566332551868 / 353203115728).
OCT7_CAMERON_FILE_ID = "118kfFlx5neGUHJKUW1ZjobS58TIyM3ng"
OCT7_GROK_SEO_BODY = (
    "Josh and Cameron talked about Grok bots and an SEO friend who might help "
    "with the site. They walked the tech stack and pricing for the bot work. "
) * 4
DAY_JOB_BODY = (
    "Insight Cisco Meraki discussion about the Okta renewal quote and the "
    "Microsoft to Microsoft migration. Pricing around one hundred fifty thousand. "
) * 4
POC_PRICING_BODY = (
    "We kicked off a POC on the Microsoft migration and talked pricing around "
    "$150k with their CFO. Contract renewal next quarter. "
) * 4
SG_DISCO_BODY = (
    "This is a SalesGlider discovery call about their roofing pipeline and campaign. "
    "They asked about the monthly retainer after we walk the owners. "
) * 3


def _cube(
    *,
    external_id: str,
    name: str = "Pat Lee",
    phone: str = "+15559876543",
    email: str = "",
    transcript: str,
    raw_subject: str = "",
    first: str = "",
    last: str = "",
) -> Engagement:
    parts = name.split(" ", 1)
    return Engagement(
        source="cube_acr",
        external_id=external_id,
        phone=phone,
        email=email,
        first_name=first or parts[0],
        last_name=last or (parts[1] if len(parts) > 1 else ""),
        name=name,
        transcript=transcript,
        raw_subject=raw_subject or f"{name} cube call",
        extra={"transcript_kind": "docx_transcript"},
    )


def _handle(tmp_path, ev, hs=None, settings=None, memory=None):
    settings = settings or make_settings()
    memory = memory or Memory(settings, data_dir=tmp_path)
    report = CycleReport()
    hs = hs or FakeHubSpot()
    _handle_engagement(ev, settings, hs, memory, None, report)
    return hs, memory, report


def test_cameron_hawkins_is_hardcoded_day_job():
    assert KNOWN_NON_SALES_PEOPLE["cameron hawkins"] == "day_job"
    assert KNOWN_NON_SALES_PHONES["2145466567"] == "day_job"
    by_name = heuristic_intent(
        _cube(
            external_id="cam-name",
            name="Cameron Hawkins",
            phone="+15550001111",
            transcript=POC_PRICING_BODY,
            raw_subject="Cameron Hawkins pricing POC",
        )
    )
    assert by_name.intent == "day_job"
    assert by_name.verdict == "no"
    by_phone = heuristic_intent(
        _cube(
            external_id="cam-phone",
            name="Unknown Caller",
            phone=CAMERON_PHONE,
            transcript=POC_PRICING_BODY,
            raw_subject="Unknown Caller POC",
        )
    )
    assert by_phone.intent == "day_job"
    assert by_phone.verdict == "no"


def test_cameron_hawkins_cube_poc_creates_nothing(tmp_path):
    ev = _cube(
        external_id="cam-live",
        name="Cameron Hawkins",
        phone=CAMERON_PHONE,
        transcript=POC_PRICING_BODY,
        raw_subject="Cameron Hawkins (+1 214-546-6567) (phone) 2026-10-05 11-47-59 - transcript.docx",
    )
    assert not cube_has_sales_intent(ev)
    assert not is_cube_business_discovery(ev)
    assert not may_create_hubspot_contact(ev)
    hs, _, report = _handle(tmp_path, ev)
    assert hs.contacts == []
    assert hs.deals == []
    assert any("day_job" in s for s in report.skipped)


def test_oct7_cameron_day_job_phone_stays_day_job_despite_tech_bots_pricing(tmp_path):
    ev = _cube(
        external_id=OCT7_CAMERON_FILE_ID,
        name="Cameron Hawkins",
        phone=CAMERON_PHONE,
        transcript=OCT7_GROK_SEO_BODY,
        raw_subject=(
            "Cameron Hawkins (+1 214-546-6567) (phone) 2026-10-07 12-14-00 - transcript.docx"
        ),
    )
    ev.occurred_at = datetime(2026, 10, 7, 17, 14, tzinfo=timezone.utc)
    decision = heuristic_intent(ev)
    assert decision.intent == "day_job"
    assert decision.verdict == "no"
    assert not cube_has_sales_intent(ev)
    assert not is_cube_business_discovery(ev)
    assert not may_create_hubspot_contact(ev)

    hs, _, report = _handle(tmp_path / "handle", ev)
    assert hs.contacts == []
    assert hs.deals == []
    assert any("day_job" in s for s in report.skipped)

    hs2 = FakeHubSpot()
    report2 = CycleReport()
    timelines = build_timelines([ev])
    apply_timeline(
        next(iter(timelines.values())),
        make_settings(),
        hs2,
        Memory(make_settings(), data_dir=tmp_path / "recon"),
        report2,
    )
    restore_missing_deals(
        hs2, make_settings(), Memory(make_settings(), data_dir=tmp_path / "restore"), report2, timelines
    )
    assert hs2.contacts == []
    assert hs2.deals == []


def test_prior_day_job_is_checked_by_phone_and_by_name(tmp_path):
    settings = make_settings()
    by_phone_memory = Memory(settings, data_dir=tmp_path / "phone")
    by_phone_memory.remember_person_intent(
        _cube(
            external_id="persist-phone",
            name="Unknown Caller",
            phone="+12145550310",
            transcript=DAY_JOB_BODY,
        ),
        IntentDecision(verdict="no", intent="day_job", confidence=0.93, reason="prior day_job"),
    )
    later_same_phone = _cube(
        external_id="later-phone",
        name="Riley Chen",
        phone="+12145550310",
        transcript=POC_PRICING_BODY,
        raw_subject="Riley Chen tech bots pricing",
    )
    prior_phone = by_phone_memory.lookup_prior_non_sales_intent(later_same_phone)
    assert prior_phone is not None
    assert prior_phone.intent == "day_job"
    hs, _, report = _handle(
        tmp_path / "phone-handle", later_same_phone, memory=by_phone_memory, settings=settings
    )
    assert hs.contacts == []
    assert hs.deals == []
    assert report.skipped or report.review_queue

    by_name_memory = Memory(settings, data_dir=tmp_path / "name")
    by_name_memory.remember_person_intent(
        _cube(
            external_id="persist-name",
            name="Sam Rivera",
            phone="",
            transcript=DAY_JOB_BODY,
        ),
        IntentDecision(verdict="no", intent="day_job", confidence=0.93, reason="prior day_job"),
    )
    later_same_name = _cube(
        external_id="later-name",
        name="Sam Rivera",
        phone="+15550009999",
        transcript=POC_PRICING_BODY,
        raw_subject="Sam Rivera Grok bots pricing",
    )
    prior_name = by_name_memory.lookup_prior_non_sales_intent(later_same_name)
    assert prior_name is not None
    assert prior_name.intent == "day_job"
    hs2, _, report2 = _handle(
        tmp_path / "name-handle", later_same_name, memory=by_name_memory, settings=settings
    )
    assert hs2.contacts == []
    assert hs2.deals == []
    assert report2.skipped or report2.review_queue

    raw_phone = Memory(settings, data_dir=tmp_path / "raw-phone")
    raw_phone._local.setdefault("review_queue", []).append(
        {
            "person_key": "+12145550311",
            "intent": "day_job",
            "confidence": 0.93,
            "reason": "review_queue raw phone",
        }
    )
    assert (
        raw_phone.lookup_prior_non_sales_intent(
            _cube(
                external_id="raw-phone",
                name="Pat Lee",
                phone="+12145550311",
                transcript=OCT7_GROK_SEO_BODY,
            )
        ).intent
        == "day_job"
    )

    raw_name = Memory(settings, data_dir=tmp_path / "raw-name")
    raw_name._local.setdefault("review_queue", []).append(
        {
            "person_key": "Cameron Hawkins",
            "intent": "personal",
            "confidence": 0.9,
            "reason": "review_queue raw name",
        }
    )
    assert (
        raw_name.lookup_prior_non_sales_intent(
            _cube(
                external_id="raw-name",
                name="Cameron Hawkins",
                phone="+15551230000",
                transcript=OCT7_GROK_SEO_BODY,
            )
        ).intent
        == "personal"
    )


def test_bare_poc_cube_without_salesglider_does_not_create(tmp_path):
    ev = _cube(
        external_id="poc-only",
        name="Riley Chen",
        phone="+12145550199",
        transcript=POC_PRICING_BODY,
        raw_subject="Riley Chen POC pricing",
    )
    assert kind_for(ev) == KIND_POC
    assert not has_salesglider_offer_talk(ev)
    assert not cube_has_sales_intent(ev)
    assert not is_confident_sales(heuristic_intent(ev))
    assert not held_call_may_open_deal(ev)
    assert not may_create_hubspot_contact(ev)
    assert resolve_stage(ev) == ""

    hs, memory, report = _handle(tmp_path / "handle", ev)
    assert hs.contacts == []
    assert hs.deals == []
    assert report.review_queue

    hs2 = FakeHubSpot()
    report2 = CycleReport()
    timelines = build_timelines([ev])
    apply_timeline(next(iter(timelines.values())), make_settings(), hs2, Memory(make_settings(), data_dir=tmp_path / "recon"), report2)
    restore_missing_deals(hs2, make_settings(), Memory(make_settings(), data_dir=tmp_path / "restore"), report2, timelines)
    assert hs2.contacts == []
    assert hs2.deals == []
    assert any("poc_hint" in x for x in report2.review_queue)


def test_prior_day_job_blocks_later_cube_sales_poc_hint(tmp_path):
    settings = make_settings()
    memory = Memory(settings, data_dir=tmp_path)
    earlier = _cube(
        external_id="cam-83",
        name="Jordan Hale",
        phone="+12145550200",
        transcript=DAY_JOB_BODY,
        raw_subject="Jordan Hale Meraki Discussion",
    )
    later = _cube(
        external_id="cam-84",
        name="Jordan Hale",
        phone="+12145550200",
        transcript=POC_PRICING_BODY,
        raw_subject="Jordan Hale POC pricing",
    )
    assert heuristic_intent(earlier).intent == "day_job"
    hs1, memory, report1 = _handle(tmp_path / "one", earlier, memory=memory, settings=settings)
    assert hs1.contacts == []
    assert hs1.deals == []
    assert memory.lookup_prior_non_sales_intent(later)
    assert memory.lookup_prior_non_sales_intent(later).intent == "day_job"

    hs2, memory, report2 = _handle(tmp_path / "two", later, memory=memory, settings=settings)
    assert hs2.contacts == []
    assert hs2.deals == []
    assert report2.skipped or report2.review_queue

    hs3 = FakeHubSpot()
    report3 = CycleReport()
    timelines = build_timelines([later])
    apply_timeline(next(iter(timelines.values())), settings, hs3, memory, report3)
    restore_missing_deals(hs3, settings, memory, report3, timelines)
    assert hs3.contacts == []
    assert hs3.deals == []


def test_same_cycle_day_job_wins_over_later_poc_sales():
    settings = make_settings()
    earlier = _cube(
        external_id="same-dj",
        name="Morgan Tate",
        phone="+12145550201",
        transcript=DAY_JOB_BODY,
        raw_subject="Morgan Tate Meraki Discussion",
    )
    later = _cube(
        external_id="same-poc",
        name="Morgan Tate",
        phone="+12145550201",
        transcript=POC_PRICING_BODY,
        raw_subject="Morgan Tate POC",
    )
    attach_person_intent(settings, [later, earlier])
    assert later._person_intent.intent == "day_job"
    assert is_confident_non_sales(later._person_intent)


def test_salesglider_cube_discovery_still_creates(tmp_path):
    ev = _cube(
        external_id="sg-disco",
        name="Rob Lawson",
        phone="+15559876543",
        email="rob@cyberguard360.com",
        transcript=SG_DISCO_BODY,
        raw_subject="2026-09-03 SalesGlider Intro",
    )
    assert has_salesglider_offer_talk(ev)
    assert cube_has_sales_intent(ev)
    assert is_cube_business_discovery(ev)
    assert may_create_hubspot_contact(ev)
    hs, _, report = _handle(tmp_path, ev)
    assert hs.contacts
    assert hs.deals
    assert hs.deals[0]["properties"]["dealstage"] == STAGE["discovery_completed"]
    assert report.contacts_upserted


def test_tyler_poc_hint_still_reviews_existing_deal(tmp_path):
    ev = Engagement(
        source="fireflies",
        external_id="ff-tyler-poc",
        email="tyler@deeprootscapital.com",
        first_name="Tyler",
        last_name="Leverington",
        company="Deep Roots Capital",
        transcript="Tyler and Josh kicked off the paid POC. Onboarding starts this week.",
        raw_subject="Tyler Leverington POC kickoff",
    )
    assert kind_for(ev) == KIND_POC
    hs = FakeHubSpot(
        [
            {
                "id": "tyler-1",
                "properties": {
                    "email": "tyler@deeprootscapital.com",
                    "firstname": "Tyler",
                    "lastname": "Leverington",
                    "company": "Deep Roots Capital",
                    "crm_source": "calendly",
                },
            }
        ]
    )
    hs.deals.append(
        {
            "id": "tyler-deal",
            "contact_id": "tyler-1",
            "properties": {
                "dealstage": STAGE["discovery_completed"],
                "dealname": "Tyler Leverington - Deep Roots Capital",
                "amount": "5000",
            },
        }
    )
    report = CycleReport()
    apply_timeline(
        build_timelines([ev])["email:tyler@deeprootscapital.com"],
        make_settings(),
        hs,
        Memory(make_settings(), data_dir=tmp_path),
        report,
    )
    assert hs.deals[0]["properties"]["dealstage"] == STAGE["discovery_completed"]
    assert hs.deals[0]["properties"]["amount"] == "5000"
    assert any("poc_hint" in x for x in report.review_queue)


def test_boyd_calendly_booking_still_sales():
    ev = Engagement(
        source="calendly",
        external_id="gcal:boyd",
        email="jboyd@boydsoftx.com",
        first_name="John",
        last_name="Boyd",
        raw_subject="SalesGlider Boyd Cold Email",
        extra={"gcal_create": True, "create_new": True, "skip_lookback": True},
    )
    assert is_confident_sales(heuristic_intent(ev))


def test_dave_ackley_deal_holder_not_day_job():
    ev = Engagement(
        source="fireflies",
        external_id="ff-dave",
        email="dave@goliath.com",
        first_name="Dave",
        last_name="Ackley",
        company="Goliath",
        raw_subject="Meraki Discussion with Insight",
        transcript="Talked about insight.com meraki and the Goliath campaign.",
    )
    stamp_deal_context(
        ev,
        {
            "id": "c-dave",
            "properties": {
                "email": "dave@goliath.com",
                "firstname": "Dave",
                "lastname": "Ackley",
                "company": "Goliath",
            },
        },
        [
            {
                "id": "d-dave",
                "contact_id": "c-dave",
                "properties": {"dealstage": STAGE["proposal_sent"], "amount": "21000"},
            }
        ],
    )
    decision = heuristic_intent(ev)
    assert decision.intent != "day_job"
    assert decision.verdict != "no"
