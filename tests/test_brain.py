from crmbrain.config import is_client_context, is_internal_meeting, is_personal
from crmbrain.http_mcp import clean_drive_id, extract_drive_ids
from crmbrain.intelligence import heuristic_extract, stage_id
from crmbrain.sources.gmail_scan import _stage_from_mail, is_josh_meeting, parse_calendly
from crmbrain.ticker import draft_email


def test_clean_drive_id():
    assert clean_drive_id("1FpCI_YwXrkmkRh8Agdc0P-womX9VNZnY-0-16") == "1FpCI_YwXrkmkRh8Agdc0P-womX9VNZnY"


def test_extract_drive_ids():
    html = "ssk='foo:1AbCdefghijklmnopqrstuvwxyZ12-0-2' data-id=\"zzzzzzzzzzzzzzzzzzzzzzzzzzzz\""
    ids = extract_drive_ids(html)
    assert ids[0].startswith("1AbC")


def test_personal_filter():
    assert is_personal(phone="+15614278965")
    assert is_personal(name="Diana Burns")
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


def test_josh_calendly_creates_contact():
    subject = "New Event: Laura Klein - 01:00pm Thu, Sep 3, 2026 - SalesGlider Intro"
    body = "Invitee: Laura Klein\nInvitee Email: lklein@grnplano.com\nEvent Type: SalesGlider Intro\n"
    cal = parse_calendly(subject, body)
    assert cal["email"] == "lklein@grnplano.com"
    assert cal["first_name"] == "Laura"
    assert cal["domain"] == "grnplano.com"
    assert is_josh_meeting(subject, cal["event_type"])
    assert not is_josh_meeting("New Event: Random Lead", "Client Roofing Campaign")


def test_nurture_copy_has_no_dashes():
    subject, body = draft_email("Jackie Darkazalli", "Kelly Roofing", "kicked_can")
    assert "—" not in subject + body
    assert "–" not in subject + body
    assert "worth sharing more?" in body
