from datetime import datetime, timedelta

from crmbrain.briefing import due_to_send, format_phone, format_when, matches_sent_brief, render
from crmbrain.enrichment import _apply_row, _rows_from_result
from crmbrain.config import CDT, is_client_context, is_internal_meeting, is_personal
from crmbrain.http_mcp import clean_drive_id, extract_drive_ids
from crmbrain.intelligence import heuristic_extract, stage_id
from crmbrain.leadmagic import (
    parse_email_response,
    parse_mobile_response,
    should_skip_email,
    usable_linkedin,
)
from crmbrain.models import Engagement
from crmbrain.sources.gmail_scan import (
    _stage_from_mail,
    counterpart_from_headers,
    is_josh_meeting,
    is_system_address,
    parse_calendly,
    parse_meeting_at,
)
from crmbrain.ticker import draft_email, infer_industry


def test_clean_drive_id():
    assert clean_drive_id("1FpCI_YwXrkmkRh8Agdc0P-womX9VNZnY-0-16") == "1FpCI_YwXrkmkRh8Agdc0P-womX9VNZnY"


def test_extract_drive_ids():
    html = "ssk='foo:1AbCdefghijklmnopqrstuvwxyZ12-0-2' data-id=\"zzzzzzzzzzzzzzzzzzzzzzzzzzzz\""
    ids = extract_drive_ids(html)
    assert ids[0].startswith("1AbC")


def test_personal_filter():
    assert is_personal(phone="+15614278965")
    assert is_personal(name="Diana Burns")
    assert is_personal(name="Jeremy Ciotola")
    assert is_personal(name="Sarah Osborn")
    assert is_personal(phone="+19733030001")
    assert not is_personal(email="rob@cyberguard360.com", name="Robert Lawson")


def test_client_context():
    assert is_client_context(name="Kyle Peterson", title="Kyle Peterson/Josh Osborn")
    assert not is_client_context(name="Robert Lawson", company="CyberGuard360")


def test_internal_meeting():
    assert is_internal_meeting("Weekly pipeline", ["joshua@salesglidergrowth.com"])
    assert not is_internal_meeting("Robert Lawson and Joshua Osborn", ["rob@cyberguard360.com"])


def test_stage_signals():
    facts = heuristic_extract("Let's circle back next quarter after their son starts at Baylor University.")
    assert facts["ticker_reason"] == "kicked_can"
    assert "Baylor" in (facts["relationship_hooks"] or "")
    assert "son" in (facts["family_notes"] or "").lower()
    assert stage_id("paid") == "3482933986"
    loose = heuristic_extract("If they signed and said let's do it, that still is not a closed deal.")
    assert loose.get("stage_hint") != "signed"


def test_gmail_pandadoc_and_calendly():
    assert _stage_from_mail("Document completed", "PandaDoc", "has been signed") == "closedwon"
    assert _stage_from_mail("New Event", "Calendly", "accepted") == "qualifiedtobuy"
    assert _stage_from_mail("Invitee no-show", "Calendly", "no-show") == "3557889773"
    assert _stage_from_mail("Invitation: SalesGlider Intro", "calendar-notification@google.com", "scheduled") == (
        "qualifiedtobuy"
    )


def test_josh_calendly_creates_contact():
    subject = "New Event: Laura Klein - 01:00pm Thu, Sep 3, 2026 - SalesGlider Intro"
    body = "Invitee: Laura Klein\nInvitee Email: lklein@grnplano.com\nEvent Type: SalesGlider Intro\n"
    cal = parse_calendly(subject, body)
    assert cal["email"] == "lklein@grnplano.com"
    assert cal["first_name"] == "Laura"
    assert cal["domain"] == "grnplano.com"
    assert is_josh_meeting(subject, cal["event_type"])
    assert not is_josh_meeting("New Event: Random Lead", "Client Roofing Campaign")
    meeting = parse_meeting_at(subject, body)
    assert meeting is not None
    assert meeting.astimezone(CDT).hour == 13
    assert meeting.astimezone(CDT).day == 3
    assert meeting.astimezone(CDT).month == 9


def test_one_brief_two_hours_before():
    meeting = datetime(2026, 9, 3, 13, 0, tzinfo=CDT)
    assert due_to_send(meeting, meeting - timedelta(hours=2))
    assert due_to_send(meeting, meeting - timedelta(hours=1))
    assert not due_to_send(meeting, meeting - timedelta(hours=6))
    assert not due_to_send(meeting, meeting - timedelta(minutes=30))
    assert not due_to_send(meeting, meeting + timedelta(hours=1))
    ev = Engagement(
        source="brief",
        external_id="laura",
        first_name="Laura",
        last_name="Klein",
        email="lklein@grnplano.com",
        company="GRN Plano Executive Search",
        title="President and Managing Partner",
        phone="4697011712",
        linkedin_url="https://www.linkedin.com/in/lauramklein",
        extra={
            "event_type": "SalesGlider Intro",
            "meeting_at": meeting,
            "pain": "Boutique executive search. Healthcare medical specialties, health insurance, medical devices, healthcare innovation, consumer products. Fixed-fee model vs traditional retained search.",
            "personal": "Works healthcare / MedTech / health insurance recruiting out of Plano. Site grnplanoexecsearch.com.",
        },
    )
    body = render(ev)
    assert body.startswith("Brief: Laura Klein")
    assert "Company: GRN Plano Executive Search" in body
    assert "Phone: 4697011712" in body
    assert "Why you are talking" in body
    assert "Thu Sep 3 2026 1:00pm CDT" in body
    assert "Offer that fits" in body
    assert format_phone("+1 (469) 701-1712") == "4697011712"
    assert "1:00pm CDT" in format_when(meeting)
    sent = render(ev)
    assert matches_sent_brief("Brief: Laura Klein", sent, ev)
    assert not matches_sent_brief("Brief: Someone Else", "unrelated", ev)


def test_leadmagic_parsers_and_guards():
    assert parse_email_response({"status": "valid", "email": "Bo@Example.com"}) == "bo@example.com"
    assert parse_email_response({"status": "not_found", "email": "x@y.com"}) == ""
    assert parse_mobile_response({"mobile_number": "4697011712"}) == "+14697011712"
    assert parse_mobile_response({"mobile_number": None}) == ""
    assert usable_linkedin("https://www.linkedin.com/in/dnyanoba-mulgir-93118588") == ""
    assert usable_linkedin("https://www.linkedin.com/in/lauramklein")
    assert usable_linkedin("lauramklein") == "https://www.linkedin.com/in/lauramklein"
    assert should_skip_email("booking-bridge-sync-test@salesglidergrowth.com")
    assert not should_skip_email("lklein@grnplano.com")


def test_waterfall_row_supplies_linkedin():
    rows = _rows_from_result(
        {"status": "completed", "result": {"contacts": [{"email": "lklein@grnplano.com", "linkedin_url": "https://www.linkedin.com/in/lauramklein"}]}}
    )
    assert rows[0]["linkedin_url"].endswith("lauramklein")
    ev = Engagement(source="test", external_id="1", email="lklein@grnplano.com")
    ev = _apply_row(ev, rows[0])
    assert ev.linkedin_url == "https://www.linkedin.com/in/lauramklein"


def test_gmail_counterpart_is_the_other_person():
    first, last, email = counterpart_from_headers(
        "Joshua Osborn <joshua@salesglidergrowth.com>",
        "Laura Klein <lklein@grnplano.com>",
    )
    assert email == "lklein@grnplano.com"
    assert first == "Laura"
    assert last == "Klein"
    inbox_first, inbox_last, inbox_email = counterpart_from_headers(
        "Laura Klein <lklein@grnplano.com>",
        "joshua@salesglidergrowth.com",
    )
    assert inbox_email == "lklein@grnplano.com"
    assert is_system_address("noreply@calendly.com")
    assert is_system_address("joshua@salesglidergrowth.com")
    assert not is_system_address("lklein@grnplano.com")


def test_nurture_copy_has_no_dashes():
    subject, body = draft_email("Jackie Darkazalli", "Kelly Roofing", "kicked_can")
    assert "—" not in subject + body
    assert "–" not in subject + body
    assert "Josh Osborn" in body


def test_nurture_roofing_uses_roi_case_study():
    subject, body = draft_email("Jackie Darkazalli", "Kelly Roofing", "kicked_can")
    assert subject == "Roofing?"
    assert "roofing pipeline" in body.lower() or "roofers" in body.lower()
    assert "$2M" in body
    assert "$100K" in body
    assert "14+" in body
    assert "AirPods" in body
    assert "Josh Osborn" in body
    assert "Quick bump" not in subject
    assert infer_industry("Kelly Roofing")["key"] == "roofing"


def test_nurture_hvac_from_company_or_campaign():
    subject, body = draft_email("Joel Stewart", "The Chill Brothers", "never_booked")
    assert subject == "Quick update"
    assert "pipeline last quarter" in body
    assert "14+" in body

    subject, body = draft_email(
        "Joel Stewart",
        "The Chill Brothers",
        "never_booked",
        extras={"campaign_name": "SG HVAC owners"},
    )
    assert subject == "HVAC update"
    assert "what we're doing in HVAC" in body
    assert "HVAC clients" in body
    assert "$100K" in body
    assert "free 10K" in body
    assert infer_industry("The Chill Brothers", extras={"vertical": "HVAC"})["key"] == "hvac"


def test_nurture_generalized_when_industry_unknown():
    subject, body = draft_email("Pat Lee", "Acme Holdings", "never_booked")
    assert subject == "Quick update"
    assert infer_industry("Acme Holdings") is None
    assert "$2M" in body and "pipeline last quarter" in body
    assert "$100K" in body and "first 3 months" in body
    assert "14+" in body
    assert "Loom" in body
    assert "10K" in body
    assert "AirPods" in body
    assert "Josh Osborn" in body
    assert "HVAC" not in body
    assert "roofing" not in body.lower()
    no_show_subject, _ = draft_email("Pat Lee", "Acme Holdings", "no_show")
    assert no_show_subject == "Pat"


def test_nurture_industry_from_title_and_explicit():
    assert infer_industry("GRN Plano", extras={"title": "President, Executive Search"})["key"] == "staffing"
    assert infer_industry("CyberGuard360")["key"] == "msp"
    subject, body = draft_email("Rob", "Northside GC", extras={"industry": "construction"})
    assert subject == "Construction update"
    assert "construction" in body.lower()
