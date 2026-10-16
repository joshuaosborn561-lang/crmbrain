"""Completed PandaDoc: POC → Signed, renewals → Renewed, freeze-exempt, call TCV."""

from __future__ import annotations

from datetime import datetime, timezone

from crmbrain.budget import WriteBudget
from crmbrain.config import RENEWAL_PIPELINE, RENEWAL_STAGE, STAGE
from crmbrain.cycle import apply_gmail_stage_update
from crmbrain.memory import Memory
from crmbrain.models import CycleReport, Engagement
from crmbrain.names import companies_fuzzy_match
from crmbrain.policy import choose_deal_action, event_predates_freeze, should_move_stage
from crmbrain.sources.gmail_scan import scan as scan_gmail
from tests.test_crm_gating import FakeHubSpot, make_settings
from tests.test_qa_fixes import FakeGmail

FREEZE = datetime(2026, 10, 3, 1, 30, tzinfo=timezone.utc)
HELD_AT = datetime(2026, 9, 20, 17, 0, tzinfo=timezone.utc)
DOC_AT_MS = str(int(datetime(2026, 9, 28, 18, 0, tzinfo=timezone.utc).timestamp() * 1000))
GOLIATH_HELD = (
    "Dave walked the Goliath360 campaign. They agreed $3,200/mo for 9 months "
    "(9 x $3,200)."
)


def test_companies_fuzzy_match_goliath360_to_hubspot_name():
    assert companies_fuzzy_match("Goliath360", "Goliath Cyber Security Group")
    assert companies_fuzzy_match("Goliath Cyber Security Group", "Goliath360")
    assert not companies_fuzzy_match("Goliath360", "Bolder Cyber Partners")


def test_signed_contract_supersedes_poc_rank():
    assert STAGE["poc"]  # rank 7
    assert not should_move_stage(STAGE["poc"], STAGE["proposal_sent"])
    assert should_move_stage(STAGE["poc"], STAGE["contract_signed_unpaid"])
    assert should_move_stage(STAGE["poc"], STAGE["closed_won"])
    ev = Engagement(
        source="gmail",
        external_id="pd-goliath",
        email="dave@goliath.com",
        raw_subject="Document completed",
        extra={"completed_doc": True, "document_name": "SalesGlider - Goliath360", "from": "PandaDoc"},
    )
    assert (
        choose_deal_action(STAGE["poc"], STAGE["contract_signed_unpaid"], ev)
        == STAGE["contract_signed_unpaid"]
    )
    assert WriteBudget().classify("move", STAGE["poc"], STAGE["contract_signed_unpaid"]) == "stage_move"


def test_pandadoc_is_not_blocked_by_manual_freeze():
    settings = make_settings(manual_freeze_at=FREEZE)
    ev = Engagement(
        source="gmail",
        external_id="pd-pre-freeze",
        email="dave@goliath.com",
        raw_subject="Document completed: Goliath360",
        occurred_at=datetime(2026, 9, 28, tzinfo=timezone.utc),
        extra={
            "completed_doc": True,
            "document_name": "SalesGlider Growth Partners SOW - Goliath360",
            "from": "PandaDoc <noreply@pandadoc.com>",
        },
    )
    assert event_predates_freeze(ev, settings) is False


def test_goliath_completed_pandadoc_moves_poc_to_signed_with_call_tcv(tmp_path):
    settings = make_settings(manual_freeze_at=FREEZE)
    contact = {
        "id": "c-dave",
        "properties": {
            "email": "dave@goliath.com",
            "firstname": "Dave",
            "lastname": "Ackley",
            "company": "Goliath Cyber Security Group",
        },
    }
    hs = FakeHubSpot([contact])
    hs.settings = settings
    hs.deals.append(
        {
            "id": "d-goliath",
            "contact_id": "c-dave",
            "properties": {
                "dealname": "Dave Ackley - Goliath",
                "dealstage": STAGE["poc"],
                "pipeline": "default",
            },
        }
    )
    gmail = FakeGmail(
        [
            {
                "id": "pd-goliath",
                "internalDate": DOC_AT_MS,
                "headers": {
                    "from": "PandaDoc <noreply@pandadoc.com>",
                    "to": "joshua@salesglidergrowth.com",
                    "subject": "Document completed: SalesGlider Growth Partners SOW - Goliath360",
                },
                "snippet": "has been completed",
                "body": (
                    "SalesGlider Growth Partners SOW - Goliath360 has been completed. "
                    "Dave Ackley (dave@goliath.com) has signed the document."
                ),
            }
        ]
    )
    report = CycleReport()
    evs = scan_gmail(settings, gmail, hs, report)
    assert evs
    ev = evs[0]
    assert ev.email == "dave@goliath.com"
    assert ev.extra.get("completed_doc") is True
    assert "Goliath360" in (ev.company or ev.extra.get("document_name") or "")
    held = Engagement(
        source="fireflies",
        external_id="ff-goliath",
        email="dave@goliath.com",
        first_name="Dave",
        last_name="Ackley",
        company="Goliath Cyber Security Group",
        occurred_at=HELD_AT,
        transcript=GOLIATH_HELD,
        extra={"has_sentences": True, "sentence_count": 12},
    )
    apply_gmail_stage_update(
        ev,
        settings,
        hs,
        Memory(settings, data_dir=tmp_path),
        None,
        CycleReport(),
        held_events=[held],
    )
    deal = hs.deals[0]
    assert deal["properties"]["dealstage"] == STAGE["contract_signed_unpaid"]
    assert deal["properties"].get("amount") in {"28800", "28800.0", "28800.00"}


def test_goliath_matches_document_company_without_signer_email(tmp_path):
    settings = make_settings()
    contact = {
        "id": "c-dave",
        "properties": {
            "email": "dave@goliath.com",
            "firstname": "Dave",
            "lastname": "Ackley",
            "company": "Goliath Cyber Security Group",
        },
    }
    hs = FakeHubSpot([contact])
    gmail = FakeGmail(
        [
            {
                "id": "pd-goliath-co",
                "headers": {
                    "from": "PandaDoc <docs@email.getpandadoc.com>",
                    "to": "joshua@salesglidergrowth.com",
                    "subject": "Document completed: SalesGlider Growth Partners SOW - Goliath360",
                },
                "snippet": "has been completed",
                "body": "SalesGlider Growth Partners SOW - Goliath360 has been completed.",
            }
        ]
    )
    evs = scan_gmail(settings, gmail, hs, CycleReport())
    assert evs
    assert evs[0].email == "dave@goliath.com"
    assert evs[0].extra.get("hubspot_contact_id") == "c-dave"


def test_bolder_completed_pandadoc_moves_call_scheduled_to_renewed(tmp_path):
    settings = make_settings(manual_freeze_at=FREEZE)
    contact = {
        "id": "c-mike",
        "properties": {
            "email": "mike@boldercyberpartners.com",
            "firstname": "Mike",
            "lastname": "Trpkosh",
            "company": "Bolder Cyber Partners",
        },
    }
    hs = FakeHubSpot([contact])
    hs.settings = settings
    renewal = {
        "id": "r-bolder",
        "contact_id": "c-mike",
        "properties": {
            "dealname": "Bolder - Renewal",
            "dealstage": RENEWAL_STAGE["call_scheduled"],
            "pipeline": RENEWAL_PIPELINE,
        },
    }
    hs.deals.append(renewal)
    gmail = FakeGmail(
        [
            {
                "id": "pd-bolder",
                "internalDate": DOC_AT_MS,
                "headers": {
                    "from": "PandaDoc <noreply@pandadoc.com>",
                    "to": "joshua@salesglidergrowth.com",
                    "subject": "Document completed: SalesGlider Growth Partners Agreement - Bolder",
                },
                "snippet": "has been completed",
                "body": (
                    "SalesGlider Growth Partners Agreement - Bolder has been completed. "
                    "Mike Trpkosh (mike@boldercyberpartners.com) has signed the document."
                ),
            }
        ]
    )
    evs = scan_gmail(settings, gmail, hs, CycleReport())
    assert evs
    ev = evs[0]
    assert ev.email == "mike@boldercyberpartners.com"
    apply_gmail_stage_update(
        ev,
        settings,
        hs,
        Memory(settings, data_dir=tmp_path),
        None,
        CycleReport(),
    )
    assert hs.deals[0]["properties"]["dealstage"] == RENEWAL_STAGE["renewed"]
    assert hs.deals[0]["properties"]["pipeline"] == RENEWAL_PIPELINE
