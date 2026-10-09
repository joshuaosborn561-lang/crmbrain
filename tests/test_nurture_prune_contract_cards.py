"""Oct 9 2026: nurture prune, card company/subject, contract-sent evidence."""

from __future__ import annotations

from datetime import datetime, timezone

from crmbrain.config import RENEWAL_PIPELINE, STAGE
from crmbrain.cycle import (
    _amount_from_matching_held_call,
    _apply_transcript_intelligence,
    _handle_engagement,
    apply_gmail_stage_update,
)
from crmbrain.documents import (
    company_from_document_name,
    emails_from_signature_mail,
    is_payment_link_mail,
    stage_from_signature_mail,
    viewer_name_from_text,
)
from crmbrain.intelligence import heuristic_deal_terms, tcv_from_terms
from crmbrain.memory import Memory
from crmbrain.models import CycleReport, Engagement
from crmbrain.nurture import (
    build_nurture_card,
    card_people_header,
    company_label_from_domain,
    compose_nurture_draft,
    compose_nurture_subject,
    resolve_nurture_company,
)
from crmbrain.policy import (
    contact_has_protected_deal,
    is_bot_or_junk_identity,
    may_prune_unengaged_from_source,
)
from crmbrain.prune import archive_unengaged_contact, has_live_meeting_evidence
from crmbrain.sources.gmail_scan import _stage_from_mail, mail_queries, scan as scan_gmail
from tests.test_crm_gating import FakeHubSpot, make_settings
from tests.test_qa_fixes import FakeGmail


def _nurture_contact(**props):
    row = {
        "id": props.pop("id", "c-lionel"),
        "properties": {
            "email": "lionel@empireroofing.com",
            "firstname": "Lionel",
            "lastname": "Francis",
            "company": "Empire Roofing",
            **props,
        },
    }
    return row


def test_nurture_and_closed_won_and_renewal_deals_are_protected():
    nurture = {"id": "d1", "properties": {"dealstage": STAGE["nurture"], "pipeline": "default"}}
    won = {"id": "d2", "properties": {"dealstage": STAGE["closed_won"], "pipeline": "default"}}
    lost = {"id": "d3", "properties": {"dealstage": STAGE["closed_lost"], "pipeline": "default"}}
    client = {
        "id": "d4",
        "properties": {"dealstage": "4391699185", "pipeline": RENEWAL_PIPELINE},
    }
    booked = {"id": "d5", "properties": {"dealstage": STAGE["meeting_booked"], "pipeline": "default"}}
    assert contact_has_protected_deal([nurture])
    assert contact_has_protected_deal([won])
    assert contact_has_protected_deal([client])
    assert contact_has_protected_deal([booked])
    assert not contact_has_protected_deal([lost])
    assert not contact_has_protected_deal([])


def test_gmail_person_nurture_outbound_does_not_prune(tmp_path):
    contact = _nurture_contact()
    hs = FakeHubSpot([contact])
    hs.deals.append(
        {
            "id": "320816514767",
            "contact_id": "c-lionel",
            "properties": {
                "dealname": "Lionel Francis - Empire Roofing",
                "dealstage": STAGE["nurture"],
                "pipeline": "default",
            },
        }
    )
    ev = Engagement(
        source="gmail_person",
        external_id="nurture-send-lionel",
        email="lionel@empireroofing.com",
        first_name="Lionel",
        last_name="Francis",
        company="Empire Roofing",
        raw_subject="Empire Roofing follow up",
        occurred_at=datetime(2026, 10, 8, 22, 23, tzinfo=timezone.utc),
    )
    assert not may_prune_unengaged_from_source(ev.source)
    assert not has_live_meeting_evidence(hs, contact, hs.deals)
    report = CycleReport()
    _handle_engagement(ev, make_settings(), hs, Memory(make_settings(), data_dir=tmp_path), None, report)
    assert not any(w[0] == "archive_contact" for w in hs.writes)
    assert not any("lionel@empireroofing.com" in x for x in report.contacts_pruned)
    assert any(c["id"] == "c-lionel" for c in hs.contacts)
    assert hs.deals[0]["id"] == "320816514767"


def test_archive_unengaged_refuses_protected_nurture_deal():
    contact = _nurture_contact(id="c-mike")
    contact["properties"]["email"] = "mike@roofrivercity.com"
    contact["properties"]["firstname"] = "Mike"
    contact["properties"]["lastname"] = "Dolan"
    hs = FakeHubSpot([contact])
    hs.deals.append(
        {
            "id": "320816514765",
            "contact_id": "c-mike",
            "properties": {"dealstage": STAGE["nurture"], "dealname": "Mike Dolan"},
        }
    )
    report = CycleReport()
    archive_unengaged_contact(hs, contact, report, "no meeting")
    assert not any(w[0] == "archive_contact" for w in hs.writes)
    assert report.contacts_pruned == []


def test_rvm_without_deal_still_prunes_leftover_contact(tmp_path):
    contact = {
        "id": "c-leftover",
        "properties": {
            "email": "pat@leftover.test",
            "firstname": "Pat",
            "lastname": "Leftover",
            "crm_source": "rvm",
        },
    }
    hs = FakeHubSpot([contact])
    ev = Engagement(
        source="rvm",
        external_id="rvm-left",
        email="pat@leftover.test",
        phone="+15551234567",
        first_name="Pat",
        last_name="Leftover",
        summary="RVM callback",
    )
    assert may_prune_unengaged_from_source(ev.source)
    report = CycleReport()
    _handle_engagement(ev, make_settings(), hs, Memory(make_settings(), data_dir=tmp_path), None, report)
    assert any(w[0] == "archive_contact" and w[1] == "c-leftover" for w in hs.writes)
    assert any("pat@leftover.test" in x for x in report.contacts_pruned)


def test_notetaker_still_prunes_and_upsert_refuses_create():
    assert is_bot_or_junk_identity("fred@fireflies.ai", "Fireflies Notetaker")
    hs = FakeHubSpot()
    ev = Engagement(
        source="fireflies",
        external_id="ff-fred",
        email="fred@fireflies.ai",
        first_name="Fireflies",
        last_name="Notetaker",
        name="Fireflies Notetaker",
    )
    row = hs.upsert_contact(ev)
    assert row.get("skipped") == "notetaker"
    assert not hs.contacts
    assert any(w[0] == "upsert_contact_skipped" for w in hs.writes)


def test_card_company_falls_back_to_associated_then_domain():
    assert company_label_from_domain("westroofgroup.com") == "West Roof Group"
    assert resolve_nurture_company("", "", "george@westroofgroup.com") == "West Roof Group"
    assert (
        resolve_nurture_company("", "", "elebolo@wrsroof.com", "West Roofing Solutions")
        == "West Roofing Solutions"
    )
    assert resolve_nurture_company("The Roof Docs", "", "ryan@wrsroof.com") == "The Roof Docs"
    george = {
        "name": "George Stradiota",
        "email": "george@westroofgroup.com",
        "company": "",
        "reason": "met",
        "met": True,
        "meeting_at": "2026-04-30T17:00:00+00:00",
    }
    draft = compose_nurture_draft(george)
    assert draft.subject == "West Roof Group follow up"
    assert draft.subject != "Following up"
    header = card_people_header(george)
    assert "George Stradiota (West Roof Group)" in header
    card = build_nurture_card(george, draft)
    preview = card["blocks"][3]["text"]["text"]
    assert "*Subject:* West Roof Group follow up" in preview


def test_card_never_emits_bare_following_up():
    assert compose_nurture_subject({"name": "Josh Pugmire", "email": "josh.pugmire@awardco.com"}) == (
        "Awardco follow up"
    )
    assert "Following up" not in compose_nurture_subject(
        {"name": "Josh Pugmire", "email": "josh.pugmire@awardco.com"}
    )
    assert compose_nurture_subject({"name": "Jonathan Matthews", "email": "jonathan@topa.io"}) == (
        "Topa follow up"
    )
    assert compose_nurture_subject({"name": "Morgan Pike"}) == "Morgan follow up"
    assert compose_nurture_subject({"name": "Morgan Pike"}) != "Following up"


def test_pandadoc_viewed_and_payment_link_are_contract_sent():
    stage, _amt, name = stage_from_signature_mail(
        "Christian Batten has viewed SalesGlider Growth Partners SOW - BPL Technology Group",
        "PandaDoc <docs@email.getpandadoc.com>",
        "Christian Batten has viewed the document",
        "Christian Batten has viewed the document.",
    )
    assert stage == STAGE["contract_signed_unpaid"]
    assert name
    company = company_from_document_name(name) or company_from_document_name(
        "SalesGlider Growth Partners SOW - BPL Technology Group"
    )
    assert "BPL" in company
    assert viewer_name_from_text(
        "Christian Batten has viewed the document",
        "Christian Batten has viewed the document.",
    ) == "Christian Batten"
    assert _stage_from_mail(
        "Christian Batten has viewed the document",
        "docs@email.getpandadoc.com",
        "has viewed the document",
    ) == STAGE["contract_signed_unpaid"]
    assert is_payment_link_mail(
        "HubSpot payment",
        "joshua@salesglidergrowth.com",
        "Here's your payment link",
        "Pay here: https://app.hubspot.com/payments/abc pay.hubspot.com",
    )
    assert _stage_from_mail(
        "HubSpot payment",
        "joshua@salesglidergrowth.com",
        "Here's your payment link https://pay.hubspot.com/x",
    ) == STAGE["contract_signed_unpaid"]
    queries = " ".join(mail_queries(make_settings()))
    assert "getpandadoc.com" in queries
    assert "payment link" in queries


def test_pandadoc_viewed_matches_deal_contact_and_moves_from_discovery(tmp_path):
    contact = {
        "id": "c-chris",
        "properties": {
            "email": "christian@bpltg.com",
            "firstname": "Christian",
            "lastname": "Batten",
            "company": "BPL Technology Group",
        },
    }
    hs = FakeHubSpot([contact])
    hs.deals.append(
        {
            "id": "353344762598",
            "contact_id": "c-chris",
            "properties": {
                "dealname": "Christian Batten - BPL Technology Group",
                "dealstage": STAGE["discovery_held"],
                "pipeline": "default",
            },
        }
    )
    report = CycleReport()
    gmail = FakeGmail(
        [
            {
                "id": "pd-view",
                "headers": {
                    "from": "PandaDoc <docs@email.getpandadoc.com>",
                    "to": "joshua@salesglidergrowth.com",
                    "subject": "Christian Batten has viewed SalesGlider Growth Partners SOW - BPL Technology Group",
                },
                "snippet": "Christian Batten has viewed the document",
                "body": "Christian Batten (christian@bpltg.com) has viewed the document.",
            }
        ]
    )
    evs = scan_gmail(make_settings(), gmail, hs, report)
    assert evs
    assert evs[0].email == "christian@bpltg.com"
    assert evs[0].stage_hint == STAGE["contract_signed_unpaid"]
    apply_gmail_stage_update(
        evs[0],
        make_settings(),
        hs,
        Memory(make_settings(), data_dir=tmp_path),
        None,
        CycleReport(),
    )
    assert hs.deals[0]["properties"]["dealstage"] == STAGE["contract_signed_unpaid"]


def test_agreed_monthly_times_term_is_tcv_and_writes_amount(tmp_path):
    text = "Christian agreed $2,000/mo for 6 months on the retainer."
    terms = heuristic_deal_terms(text)
    assert terms["monthly_fee"] == "2000"
    assert terms["term_months"] == "6"
    assert tcv_from_terms(terms) == "12000"
    contact = {
        "id": "c-chris",
        "properties": {
            "email": "christian@bpltg.com",
            "firstname": "Christian",
            "lastname": "Batten",
            "company": "BPL Technology Group",
        },
    }
    hs = FakeHubSpot([contact])
    hs.deals.append(
        {
            "id": "353344762598",
            "contact_id": "c-chris",
            "properties": {
                "dealname": "Christian Batten",
                "dealstage": STAGE["discovery_held"],
                "pipeline": "default",
            },
        }
    )
    ev = Engagement(
        source="fireflies",
        external_id="01M49724V60VPJKB0VT1ATGDYQ",
        email="christian@bpltg.com",
        first_name="Christian",
        last_name="Batten",
        company="BPL Technology Group",
        occurred_at=datetime(2026, 10, 8, 18, 0, tzinfo=timezone.utc),
        transcript=text + " We walked discovery and they are ready to move.",
        extra={"has_sentences": True, "sentence_count": 12},
    )
    report = CycleReport()
    _apply_transcript_intelligence(
        ev, make_settings(), hs, Memory(make_settings(), data_dir=tmp_path), report, contact, add_timeline_note=False
    )
    assert hs.deals[0]["properties"].get("amount") == "12000"


def test_contract_sent_gmail_uses_held_call_amount(tmp_path):
    contact = {
        "id": "c-chris",
        "properties": {
            "email": "christian@bpltg.com",
            "firstname": "Christian",
            "lastname": "Batten",
            "company": "BPL Technology Group",
        },
    }
    held = Engagement(
        source="fireflies",
        external_id="01M49724V60VPJKB0VT1ATGDYQ",
        email="christian@bpltg.com",
        transcript="They agreed $2,000/mo for 6 months.",
        extra={"has_sentences": True, "sentence_count": 8},
    )
    gmail = Engagement(
        source="gmail",
        external_id="pay-link",
        email="christian@bpltg.com",
        raw_subject="HubSpot payment link",
        stage_hint=STAGE["contract_signed_unpaid"],
        extra={"hubspot_contact_id": "c-chris"},
    )
    amount, terms = _amount_from_matching_held_call(make_settings(), gmail, contact, [held])
    assert amount == "12000"
    assert terms.get("monthly_fee") == "2000"
    hs = FakeHubSpot([contact])
    hs.deals.append(
        {
            "id": "353344762598",
            "contact_id": "c-chris",
            "properties": {"dealstage": STAGE["discovery_held"], "dealname": "Christian Batten"},
        }
    )
    apply_gmail_stage_update(
        gmail,
        make_settings(),
        hs,
        Memory(make_settings(), data_dir=tmp_path),
        None,
        CycleReport(),
        held_events=[held],
    )
    assert hs.deals[0]["properties"]["dealstage"] == STAGE["contract_signed_unpaid"]
    assert hs.deals[0]["properties"].get("amount") == "12000"


def test_signature_mail_body_email_helper():
    emails = emails_from_signature_mail(
        "Document viewed",
        "Viewer christian@bpltg.com opened SalesGlider Growth Partners SOW - BPL Technology Group",
    )
    assert "christian@bpltg.com" in emails
