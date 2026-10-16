"""Completed PandaDoc: POC → Signed, renewals → Renewed, freeze-exempt, call TCV."""

from __future__ import annotations

from datetime import datetime, timezone

from crmbrain.budget import WriteBudget
from crmbrain.config import RENEWAL_PIPELINE, RENEWAL_STAGE, STAGE
from crmbrain.cycle import apply_gmail_stage_update
from crmbrain.intelligence import heuristic_deal_terms, tcv_from_terms
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
    "Dave walked the Goliath360 campaign. They agreed $3,500/mo for 6 months "
    "= $21,000 TCV, paid in 9 net-60 installments of about $2,333 starting Dec 28."
)
GOLIATH_WRONG_INSTALLMENT = (
    "They talked payment schedule: 9 x $2,333 net-60 starting Dec 28."
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
    assert deal["properties"].get("amount") in {"21000", "21000.0", "21000.00"}
    assert deal["properties"].get("monthly_fee") in {"3500", "3500.0", "3500.00"}
    assert str(deal["properties"].get("contract_months") or "") in {"6", "6.0"}


def test_goliath_terms_are_monthly_times_term_not_installment_schedule():
    terms = heuristic_deal_terms(GOLIATH_HELD)
    assert terms["monthly_fee"] == "3500"
    assert terms["term_months"] == "6"
    assert tcv_from_terms(terms) == "21000"
    schedule = heuristic_deal_terms(GOLIATH_WRONG_INSTALLMENT)
    assert tcv_from_terms(schedule) not in {"20997", "2333", "28800"}
    assert tcv_from_terms(schedule) == ""


def test_goliath_existing_amount_is_not_overwritten(tmp_path):
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
            "id": "340447563471",
            "contact_id": "c-dave",
            "properties": {
                "dealname": "Dave Ackley - Goliath",
                "dealstage": STAGE["poc"],
                "pipeline": "default",
                "amount": "21000",
                "monthly_fee": "3500",
                "contract_months": "6",
                "manual_modified_at": "2026-10-16T12:00:00+00:00",
            },
        }
    )
    gmail = FakeGmail(
        [
            {
                "id": "pd-goliath-keep",
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
    evs = scan_gmail(settings, gmail, hs, CycleReport())
    assert evs
    held = Engagement(
        source="fireflies",
        external_id="ff-goliath-wrong",
        email="dave@goliath.com",
        first_name="Dave",
        last_name="Ackley",
        company="Goliath Cyber Security Group",
        occurred_at=HELD_AT,
        transcript=GOLIATH_WRONG_INSTALLMENT,
        extra={"has_sentences": True, "sentence_count": 12},
    )
    apply_gmail_stage_update(
        evs[0],
        settings,
        hs,
        Memory(settings, data_dir=tmp_path),
        None,
        CycleReport(),
        held_events=[held],
    )
    deal = hs.deals[0]
    assert deal["id"] == "340447563471"
    assert deal["properties"]["dealstage"] == STAGE["contract_signed_unpaid"]
    assert deal["properties"].get("amount") in {"21000", "21000.0", "21000.00"}
    assert deal["properties"].get("monthly_fee") in {"3500", "3500.0", "3500.00"}
    assert str(deal["properties"].get("contract_months") or "") in {"6", "6.0"}
    assert not any(
        w[0] == "patch_deal" and ("amount" in (w[2] or {}) or "monthly_fee" in (w[2] or {}))
        for w in hs.writes
    )


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
