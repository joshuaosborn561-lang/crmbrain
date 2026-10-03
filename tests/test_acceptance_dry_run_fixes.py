"""Acceptance fixtures for the prod dry-run that failed at 9ec05c4."""

from datetime import datetime, timezone
from io import StringIO
import logging

from crmbrain.config import (
    STAGE,
    is_non_deal_person,
    redact_secrets,
    resolve_gemini_model,
)
from crmbrain.cycle import apply_gmail_stage_update, _handle_engagement, should_reextract
from crmbrain.intelligence import (
    extract,
    josh_new_text,
    latest_proposal_figure,
    parse_deal_amount,
)
from crmbrain.memory import Memory
from crmbrain.models import CycleReport, Engagement
from crmbrain.policy import choose_deal_action, last_manual_modification
from tests.test_crm_gating import FakeHubSpot, make_settings, stub_gemini_extract


TYLER_TRANSCRIPT = """
Josh: One of our roofers closed $144,000 in pipeline. Another did $30,000 last quarter.
Tyler agreed to the $20,000 package. I'll send the proposal this week.
"""

EARL_INSTALMENT_THREAD = """
Earl — first installment is $2,222.

On Fri, Sep 12, 2026 at 4:02 PM Joshua Osborn wrote:
> Attached is the SalesGlider proposal. The engagement is $20,000.
>
> Josh
"""

VECTOR_OLD_DRAFT = """
Thanks Alvaro — looping the signed copy.

On Mon, Aug 3, 2026 Joshua Osborn wrote:
> Draft thoughts were $6,000 or $9,000 depending on term.
> I'll send the real proposal later.
"""

DONIGAN_CALL = Engagement(
    source="fireflies",
    external_id="ff-donigan-sep25",
    occurred_at=datetime(2026, 9, 25, 16, 0, tzinfo=timezone.utc),
    email="donigan@example.com",
    first_name="Donigan",
    last_name="Lead",
    transcript="We walked discovery on the roofing campaign.",
    raw_subject="Donigan and Joshua Osborn",
    extra={"has_sentences": True, "sentence_count": 6},
)


def test_gemini_lite_coerces_to_flash():
    assert resolve_gemini_model("gemini-2.5-flash-lite") == "gemini-2.5-flash"
    assert resolve_gemini_model("") == "gemini-2.5-flash"
    assert resolve_gemini_model("gemini-2.5-flash") == "gemini-2.5-flash"


def test_redact_secrets_strips_key_and_query():
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent?key=SECRET123"
    out = redact_secrets(f"404 Client Error: Not Found for url: {url}")
    assert "SECRET123" not in out
    assert "key=" not in out or "key=REDACTED" in out
    assert "?" not in out.split("url:", 1)[-1]


def test_parse_deal_amount_never_picks_largest_figure():
    assert parse_deal_amount(TYLER_TRANSCRIPT) in {"", "20000"}
    assert parse_deal_amount(TYLER_TRANSCRIPT) != "144000"
    assert parse_deal_amount(TYLER_TRANSCRIPT) != "30000"
    mixed = "Retainer $12k/mo. Also mentioned a $36,000 case study."
    assert parse_deal_amount(mixed) != "36000"


def test_josh_new_text_and_latest_figure_ignore_quoted_and_instalment():
    assert "20,000" not in josh_new_text(EARL_INSTALMENT_THREAD)
    assert latest_proposal_figure(EARL_INSTALMENT_THREAD) == "2222"
    assert "6,000" not in josh_new_text(VECTOR_OLD_DRAFT)
    assert latest_proposal_figure(VECTOR_OLD_DRAFT) == ""


def test_vector_paid_plus_proposal_email_is_no_write(tmp_path):
    ev = Engagement(
        source="gmail",
        external_id="vec-ps",
        email="agancman@vectorenergygroup.com",
        first_name="Alvaro",
        last_name="Gancman",
        company="Vector Energy Group",
        raw_subject="SalesGlider proposal",
        transcript=VECTOR_OLD_DRAFT,
        stage_hint=STAGE["proposal_sent"],
        extra={
            "amount": "6000",
            "amount_source": "proposal_email",
            "josh_sent_proposal": True,
            "hubspot_contact_id": "v1",
        },
    )
    hs = FakeHubSpot(
        [
            {
                "id": "v1",
                "properties": {
                    "email": "agancman@vectorenergygroup.com",
                    "firstname": "Alvaro",
                    "lastname": "Gancman",
                    "company": "Vector Energy Group",
                },
            }
        ]
    )
    hs.deals.append(
        {
            "id": "d-v",
            "contact_id": "v1",
            "properties": {
                "dealstage": STAGE["paid"],
                "dealname": "Vector Energy Group",
                "amount": "8500",
            },
        }
    )
    settings = make_settings()
    report = CycleReport()
    apply_gmail_stage_update(ev, settings, hs, Memory(settings, data_dir=tmp_path), None, report)
    assert len(hs.deals) == 1
    assert hs.deals[0]["properties"]["dealstage"] == STAGE["paid"]
    assert hs.deals[0]["properties"]["amount"] == "8500"
    assert report.amounts_set == []
    assert not any("proposal_sent" in (m or "") for _w, *rest in hs.writes for m in rest)


def test_tyler_sep29_stays_20000_or_unchanged(tmp_path, monkeypatch):
    stub_gemini_extract(monkeypatch, amount="20000", quote="$20,000 package", stage_hint="proposal_sent")
    ev = Engagement(
        source="fireflies",
        external_id="ff-tyler-sep29",
        occurred_at=datetime(2026, 9, 29, 18, 0, tzinfo=timezone.utc),
        email="tyler@deeprootscapital.com",
        first_name="Tyler",
        last_name="Leverington",
        transcript=TYLER_TRANSCRIPT,
        raw_subject="Tyler Leverington POC",
        extra={"has_sentences": True, "sentence_count": 4},
    )
    hs = FakeHubSpot(
        [
            {
                "id": "t1",
                "properties": {
                    "email": "tyler@deeprootscapital.com",
                    "firstname": "Tyler",
                    "lastname": "Leverington",
                },
            }
        ]
    )
    hs.deals.append(
        {
            "id": "d-t",
            "contact_id": "t1",
            "properties": {
                "dealstage": STAGE["proposal_sent"],
                "dealname": "Tyler Leverington",
                "amount": "20000",
                "manual_modified_at": "2026-10-02T00:00:00+00:00",
            },
        }
    )
    settings = make_settings(gemini_key="fake")
    report = CycleReport()
    _handle_engagement(ev, settings, hs, Memory(settings, data_dir=tmp_path), None, report)
    assert hs.deals[0]["properties"]["dealstage"] == STAGE["proposal_sent"]
    assert hs.deals[0]["properties"]["amount"] == "20000"
    assert choose_deal_action(
        STAGE["proposal_sent"], STAGE["discovery_completed"], ev, deal=hs.deals[0]
    ) is None


def test_earl_instalment_email_no_change(tmp_path):
    ev = Engagement(
        source="gmail",
        external_id="earl-inst",
        email="earl@goliath.com",
        first_name="Earl",
        last_name="Buyer",
        raw_subject="Re: SalesGlider proposal",
        transcript=EARL_INSTALMENT_THREAD,
        stage_hint=STAGE["proposal_sent"],
        extra={
            "amount": latest_proposal_figure(EARL_INSTALMENT_THREAD),
            "amount_source": "proposal_email",
            "amount_is_instalment": True,
            "josh_sent_proposal": True,
            "hubspot_contact_id": "e1",
        },
    )
    hs = FakeHubSpot(
        [{"id": "e1", "properties": {"email": "earl@goliath.com", "firstname": "Earl", "lastname": "Buyer"}}]
    )
    hs.deals.append(
        {
            "id": "d-e",
            "contact_id": "e1",
            "properties": {
                "dealstage": STAGE["proposal_sent"],
                "dealname": "Earl",
                "amount": "20000",
            },
        }
    )
    settings = make_settings()
    report = CycleReport()
    apply_gmail_stage_update(ev, settings, hs, Memory(settings, data_dir=tmp_path), None, report)
    assert hs.deals[0]["properties"]["amount"] == "20000"
    assert hs.deals[0]["properties"]["dealstage"] == STAGE["proposal_sent"]
    assert report.amounts_set == []


def test_donigan_nurture_sep25_call_does_not_move(tmp_path):
    hs = FakeHubSpot(
        [
            {
                "id": "dn1",
                "properties": {"email": "donigan@example.com", "firstname": "Donigan", "lastname": "Lead"},
            }
        ]
    )
    hs.deals.append(
        {
            "id": "d-dn",
            "contact_id": "dn1",
            "properties": {
                "dealstage": STAGE["nurture"],
                "dealname": "Donigan",
                "amount": "",
                "manual_modified_at": "2026-09-30T12:00:00+00:00",
            },
        }
    )
    settings = make_settings()
    report = CycleReport()
    _handle_engagement(DONIGAN_CALL, settings, hs, Memory(settings, data_dir=tmp_path), None, report)
    assert hs.deals[0]["properties"]["dealstage"] == STAGE["nurture"]
    assert not report.deals_moved
    assert (
        choose_deal_action(
            STAGE["nurture"], STAGE["discovery_completed"], DONIGAN_CALL, deal=hs.deals[0]
        )
        is None
    )


def test_bob_carlson_partner_never_creates(tmp_path):
    assert is_non_deal_person(name="Bob Carlson", company="Shore Capital", title="PE partner")
    ev = Engagement(
        source="fireflies",
        external_id="ff-bob",
        email="bob@shorecap.com",
        first_name="Bob",
        last_name="Carlson",
        company="Shore Capital",
        title="PE partner",
        transcript="Catch up with the PE partner on the fund.",
        raw_subject="Bob Carlson / Shore Capital",
        extra={"has_sentences": True, "sentence_count": 3},
    )
    hs = FakeHubSpot()
    settings = make_settings()
    report = CycleReport()
    _handle_engagement(ev, settings, hs, Memory(settings, data_dir=tmp_path), None, report)
    assert hs.contacts == []
    assert hs.deals == []
    assert any("excluded" in s for s in report.skipped)


def test_tj_culture_fits_paid_client_no_create(tmp_path):
    ev = Engagement(
        source="fireflies",
        external_id="ff-tj",
        email="tj@culturefits.com",
        first_name="TJ",
        last_name="Johnson",
        company="Culture Fits",
        transcript="SalesGlider intro recap with TJ on the next campaign.",
        raw_subject="TJ Johnson and Joshua Osborn",
        extra={"has_sentences": True, "sentence_count": 5},
    )
    hs = FakeHubSpot(
        [
            {
                "id": "tj1",
                "properties": {
                    "email": "tj@culturefits.com",
                    "firstname": "TJ",
                    "lastname": "Johnson",
                    "company": "Culture Fits",
                },
            },
            {
                "id": "cf-owner",
                "properties": {
                    "email": "owner@culturefits.com",
                    "firstname": "Owner",
                    "lastname": "Fits",
                    "company": "Culture Fits",
                },
            },
        ]
    )
    hs.deals.append(
        {
            "id": "d-cf",
            "contact_id": "cf-owner",
            "properties": {"dealstage": STAGE["paid"], "dealname": "Culture Fits", "amount": "12000"},
        }
    )
    settings = make_settings()
    report = CycleReport()
    _handle_engagement(ev, settings, hs, Memory(settings, data_dir=tmp_path), None, report)
    assert not any(d.get("contact_id") == "tj1" for d in hs.deals)
    assert any("notes only" in s or "client" in s for s in report.skipped) or not report.deals_moved


def test_unknown_cube_phone_goes_to_review(tmp_path):
    ev = Engagement(
        source="cube_acr",
        external_id="cu-unknown",
        phone="+16504669464",
        transcript="This is a SalesGlider discovery call about their roofing pipeline and campaign. " * 3,
        raw_subject="2026-09-29 (+16504669464)",
        extra={"transcript_kind": "docx_transcript"},
    )
    hs = FakeHubSpot()
    settings = make_settings()
    report = CycleReport()
    _handle_engagement(ev, settings, hs, Memory(settings, data_dir=tmp_path), None, report)
    assert hs.contacts == []
    assert hs.deals == []
    assert any("unknown phone" in q for q in report.review_queue)


def test_gemini_404_writes_no_amount_and_redacts_key(tmp_path, monkeypatch, caplog):
    ev = Engagement(
        source="fireflies",
        external_id="ff-404",
        email="mike@example.com",
        first_name="Mike",
        last_name="Lead",
        transcript="Mike said the monthly retainer is $3,000/month and his son plays baseball.",
        raw_subject="Mike and Joshua Osborn",
        extra={"has_sentences": True, "sentence_count": 2},
    )

    def _boom(_settings, _text):
        raise RuntimeError(
            "404 Client Error: Not Found for url: "
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-2.5-flash-lite:generateContent?key=SECRETKEY99"
        )

    monkeypatch.setattr("crmbrain.intelligence._gemini", _boom)
    settings = make_settings(gemini_key="fake")
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.WARNING)
    logger = logging.getLogger("crmbrain.intelligence")
    logger.addHandler(handler)
    with caplog.at_level(logging.WARNING):
        facts = extract(settings, ev)
        hs = FakeHubSpot(
            [{"id": "m1", "properties": {"email": "mike@example.com", "firstname": "Mike", "lastname": "Lead"}}]
        )
        hs.deals.append(
            {
                "id": "d-m",
                "contact_id": "m1",
                "properties": {"dealstage": STAGE["discovery_completed"], "amount": ""},
            }
        )
        _handle_engagement(ev, settings, hs, Memory(settings, data_dir=tmp_path), None, CycleReport())
    logger.removeHandler(handler)
    assert facts["amount_hint"] == ""
    assert hs.deals[0]["properties"].get("amount") in {"", None}
    dumped = stream.getvalue() + " ".join(r.message for r in caplog.records)
    assert "SECRETKEY99" not in dumped
    assert "key=SECRETKEY99" not in dumped


def test_reextract_preview_uses_live_gates_and_logs_current(tmp_path, monkeypatch, caplog):
    stub_gemini_extract(monkeypatch, amount="144000", quote="$144,000")
    ev = Engagement(
        source="fireflies",
        external_id="ff-re-tyler",
        occurred_at=datetime(2026, 9, 29, tzinfo=timezone.utc),
        email="tyler@deeprootscapital.com",
        first_name="Tyler",
        last_name="Leverington",
        transcript=TYLER_TRANSCRIPT,
        extra={"has_sentences": True, "sentence_count": 4},
    )
    hs = FakeHubSpot(
        [
            {
                "id": "t1",
                "properties": {
                    "email": "tyler@deeprootscapital.com",
                    "firstname": "Tyler",
                    "lastname": "Leverington",
                },
            }
        ]
    )
    hs.deals.append(
        {
            "id": "d-t",
            "contact_id": "t1",
            "properties": {
                "dealstage": STAGE["proposal_sent"],
                "dealname": "Tyler",
                "amount": "20000",
                "manual_modified_at": "2026-10-02T00:00:00+00:00",
            },
        }
    )
    live_settings = make_settings(gemini_key="fake")
    settings = make_settings(
        gemini_key="fake",
        dry_run=True,
        reextract_since=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    memory = Memory(live_settings, data_dir=tmp_path)
    memory.mark_processed("fireflies", "ff-re-tyler", {"contact_id": "t1"})
    report = CycleReport()
    assert should_reextract(settings, ev)
    with caplog.at_level(logging.INFO):
        _handle_engagement(ev, settings, hs, memory, None, report)
    assert report.proposed_writes == []
    assert any("reextract no-op" in s and "20000" in s for s in report.skipped)
    assert last_manual_modification(hs.deals[0]) is not None
