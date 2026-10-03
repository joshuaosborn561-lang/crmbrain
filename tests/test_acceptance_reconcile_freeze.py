"""Acceptance: reconcile must use the same gates as cycle. Zero writes for the 9/18 misses."""

from datetime import datetime, timezone

from crmbrain.config import STAGE, is_excluded_contact, is_non_deal_person
from crmbrain.cycle import _handle_engagement
from crmbrain.evidence import build_timelines
from crmbrain.intent import apply_deal_holder_veto, classify, heuristic_intent
from crmbrain.memory import Memory
from crmbrain.models import CycleReport, Engagement
from crmbrain.names import names_fuzzy_match
from crmbrain.policy import (
    choose_deal_action,
    deal_is_locked,
    event_predates_freeze,
    is_unidentified_cube_phone,
    may_open_new_deal,
    stamp_deal_context,
)
from crmbrain.reconcile import apply_timeline, restore_missing_deals, run as reconcile_run
from tests.test_crm_gating import FakeHubSpot, make_settings

FREEZE = datetime(2026, 10, 3, 1, 30, tzinfo=timezone.utc)
SEP_CALL = datetime(2026, 9, 25, 16, 0, tzinfo=timezone.utc)
UNKNOWN_PHONES = (
    "+16504669464",
    "+19173199171",
    "+16312561566",
    "+17372798908",
    "+15855171009",
    "+14237673636",
)


def _settings(**kwargs):
    return make_settings(manual_freeze_at=FREEZE, dry_run=True, **kwargs)


def _held(source="fireflies", **kwargs):
    defaults = dict(
        occurred_at=SEP_CALL,
        transcript="Discovery about their roofing pipeline and a proposal next week.",
        raw_subject="Discovery call",
        extra={"has_sentences": True, "sentence_count": 8},
    )
    defaults.update(kwargs)
    return Engagement(source=source, **defaults)


def _contact(cid, email="", first="", last="", company="", phone=""):
    return {
        "id": cid,
        "properties": {
            "email": email,
            "firstname": first,
            "lastname": last,
            "company": company,
            "phone": phone,
            "crm_source": "calendly",
        },
    }


def _deal(did, contact_id, stage, amount="", name="", locked=""):
    props = {"dealstage": stage, "dealname": name or did, "amount": amount}
    if locked:
        props["crmbrain_locked"] = locked
    return {"id": did, "contact_id": contact_id, "properties": props}


def test_names_fuzzy_match_mac_mc_and_typo():
    assert names_fuzzy_match("Myles MacAntosh", "Myles McAntosh")
    assert names_fuzzy_match("Myles McAntosh", "Myles MacAntosh")
    assert names_fuzzy_match("Brian Donigan", "Brian Donigan")
    assert not names_fuzzy_match("Brian Donigan", "Dan McGurl")
    assert names_fuzzy_match("Erik Pinho", "Erik Pinno")


def test_bobcbobc_email_is_not_deal():
    assert is_non_deal_person(email="bobcbobc@gmail.com")
    ev = Engagement(
        source="fireflies",
        external_id="ff-bob",
        email="bobcbobc@gmail.com",
        first_name="Bob",
        last_name="",
    )
    contact = _contact("c-bob", email="bobcbobc@gmail.com", first="Bob", last="Carlson")
    assert is_excluded_contact(ev, contact)


def test_unidentified_cube_phone_is_review_only():
    ev = Engagement(source="cube_acr", external_id="cube-1", phone="+16504669464")
    assert is_unidentified_cube_phone(ev, None)
    ok, reason = may_open_new_deal(ev, None, [], _settings())
    assert ok is False
    assert reason == "unknown_phone"


def test_freeze_and_lock_block_existing_deal_moves():
    ev = _held(external_id="ff-freeze", email="tyler@x.com", first_name="Tyler", last_name="Leverington")
    settings = _settings()
    assert event_predates_freeze(ev, settings)
    deal = _deal("d1", "c1", STAGE["proposal_sent"], amount="20000", name="Tyler")
    assert choose_deal_action(STAGE["proposal_sent"], STAGE["discovery_completed"], ev, deal, settings) is None
    locked = _deal("d2", "c1", STAGE["nurture"], locked="true")
    assert deal_is_locked(locked)
    later = _held(
        external_id="ff-later",
        occurred_at=datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc),
        email="later@x.com",
        first_name="Later",
        last_name="Lead",
    )
    assert choose_deal_action(STAGE["nurture"], STAGE["discovery_completed"], later, locked, settings) is None


def test_deal_holder_is_never_day_job_or_recruiter(tmp_path):
    dave = _held(
        external_id="ff-dave",
        email="dave@goliath.com",
        first_name="Dave",
        last_name="Ackley",
        company="Goliath",
        raw_subject="Meraki Discussion with Insight",
        transcript="Talked about insight.com meraki and the Goliath campaign.",
    )
    stamp_deal_context(
        dave,
        _contact("c-dave", "dave@goliath.com", "Dave", "Ackley", "Goliath"),
        [_deal("d-dave", "c-dave", STAGE["proposal_sent"], amount="21000")],
    )
    decision = heuristic_intent(dave)
    assert decision.intent != "day_job"
    classified = classify(make_settings(), dave)
    assert classified.intent != "day_job"
    assert classified.intent == "client_ops"

    vincent = _held(
        external_id="ff-vincent",
        email="vincent@example.com",
        first_name="Vincent",
        last_name="Bowden",
        raw_subject="Recruiter intro",
        transcript="Vincent is a recruiter talking talent acquisition.",
    )
    stamp_deal_context(
        vincent,
        _contact("c-v", "vincent@example.com", "Vincent", "Bowden"),
        [_deal("d-v", "c-v", STAGE["discovery_scheduled"])],
    )
    v_decision = apply_deal_holder_veto(vincent, heuristic_intent(vincent))
    assert v_decision.intent != "recruiter"


def test_acceptance_zero_writes_for_listed_people(tmp_path):
    settings = _settings()
    memory = Memory(settings, data_dir=tmp_path)
    hs = FakeHubSpot()
    hs.settings = settings
    report = CycleReport()

    cases = [
        (
            _held(
                external_id="ff-tyler",
                occurred_at=datetime(2026, 9, 29, 17, 0, tzinfo=timezone.utc),
                email="tyler@deeprootscapital.com",
                first_name="Tyler",
                last_name="Leverington",
                company="Deep Roots Capital",
            ),
            _contact("c-tyler", "tyler@deeprootscapital.com", "Tyler", "Leverington", "Deep Roots Capital"),
            _deal("d-tyler", "c-tyler", STAGE["proposal_sent"], amount="20000", name="Tyler"),
        ),
        (
            _held(
                external_id="ff-brian",
                email="bdonigan@wtrenovations.com",
                first_name="Brian",
                last_name="Donigan",
                company="WT Renovations",
            ),
            _contact("c-brian", "bdonigan@wtrenovations.com", "Brian", "Donigan", "WT Renovations"),
            _deal("d-brian", "c-brian", STAGE["nurture"], name="Brian Donigan"),
        ),
        (
            _held(
                external_id="ff-dan",
                email="dan@example.com",
                first_name="Dan",
                last_name="McGurl",
                raw_subject="Proposal follow up",
                transcript="I will send the proposal this week for twenty thousand.",
            ),
            _contact("c-dan", "dan@example.com", "Dan", "McGurl"),
            _deal("d-dan", "c-dan", STAGE["nurture"], name="Dan McGurl"),
        ),
        (
            _held(
                external_id="ff-erik",
                email="erik@example.com",
                first_name="Erik",
                last_name="Pinho",
            ),
            _contact("c-erik", "erik@example.com", "Erik", "Pinho"),
            _deal("d-erik", "c-erik", STAGE["nurture"], name="Erik Pinho"),
        ),
        (
            _held(
                external_id="ff-destiny",
                email="destiny@mackeymitchell.com",
                first_name="Destiny",
                last_name="Silva",
                company="Mackey Mitchell",
            ),
            _contact("c-des", "destiny@mackeymitchell.com", "Destiny", "Silva", "Mackey Mitchell"),
            _deal("d-des", "c-des", STAGE["nurture"], name="Destiny Silva"),
        ),
        (
            _held(
                external_id="ff-bob",
                email="bobcbobc@gmail.com",
                first_name="Bob",
                last_name="Carlson",
            ),
            None,
            None,
        ),
        (
            _held(
                external_id="ff-myles",
                email="",
                first_name="Myles",
                last_name="MacAntosh",
                name="Myles MacAntosh",
                company="Emcor",
            ),
            _contact("c-myles", "myles@emcor.com", "Myles", "McAntosh", "Emcor"),
            _deal("d-myles", "c-myles", STAGE["paid"], amount="12000", name="Myles McAntosh - Emcor"),
        ),
    ]

    engagements = []
    for ev, contact, deal in cases:
        if contact:
            hs.contacts.append(contact)
        if deal:
            hs.deals.append(deal)
        engagements.append(ev)

    for phone in UNKNOWN_PHONES:
        engagements.append(
            Engagement(
                source="cube_acr",
                external_id=f"cube-{phone}",
                occurred_at=SEP_CALL,
                phone=phone,
                transcript="Discovery about pricing and a proposal for their campaign.",
                raw_subject="Unknown caller",
                extra={"has_sentences": True, "sentence_count": 10},
            )
        )

    before_deals = [((d.get("properties") or {}).get("dealstage"), (d.get("properties") or {}).get("amount")) for d in hs.deals]
    reconcile_run(
        settings,
        hs,
        memory,
        report,
        engagements,
        dry_run=True,
        skip_abort=True,
    )
    for ev in engagements:
        _handle_engagement(ev, settings, hs, memory, None, report)

    after_deals = [((d.get("properties") or {}).get("dealstage"), (d.get("properties") or {}).get("amount")) for d in hs.deals]
    assert after_deals == before_deals
    labels = " ".join(
        str(x)
        for x in (
            report.proposed_writes,
            report.deals_moved,
            report.deals_restored,
            report.amounts_set,
            report.contacts_upserted,
            hs.writes,
        )
    ).lower()
    for needle in (
        "tyler",
        "donigan",
        "brian",
        "mcgurl",
        "pinho",
        "destiny",
        "bob",
        "carlson",
        "myles",
        "macantosh",
        "16504669464",
        "19173199171",
        "16312561566",
        "17372798908",
        "15855171009",
        "14237673636",
    ):
        assert needle not in labels, labels
    assert report.proposed_writes == []
    assert hs.writes == []


def test_reconcile_nurture_call_does_not_move_without_manual(tmp_path):
    settings = make_settings()  # freeze unset — choose_deal_action still blocks
    ev = _held(
        external_id="ff-dan2",
        email="dan2@example.com",
        first_name="Dan",
        last_name="McGurl",
    )
    contact = _contact("c-dan2", "dan2@example.com", "Dan", "McGurl")
    deal = _deal("d-dan2", "c-dan2", STAGE["nurture"], name="Dan")
    hs = FakeHubSpot([contact])
    hs.deals.append(deal)
    timeline = build_timelines([ev])["email:dan2@example.com"]
    timeline.contact = contact
    timeline.deals = [deal]
    report = CycleReport()
    apply_timeline(timeline, settings, hs, Memory(settings, data_dir=tmp_path), report)
    restore_missing_deals(hs, settings, Memory(settings, data_dir=tmp_path), report, {timeline.key: timeline})
    assert hs.deals[0]["properties"]["dealstage"] == STAGE["nurture"]
    assert report.proposed_writes == []
    assert report.deals_moved == []


def test_restore_unknown_phone_is_review_not_create(tmp_path):
    settings = _settings()
    ev = Engagement(
        source="cube_acr",
        external_id="cube-unk",
        occurred_at=SEP_CALL,
        phone="+16504669464",
        transcript="Discovery about pricing and a proposal for their campaign.",
        raw_subject="Unknown",
        extra={"has_sentences": True, "sentence_count": 12},
    )
    timelines = build_timelines([ev])
    hs = FakeHubSpot()
    report = CycleReport()
    restore_missing_deals(hs, settings, Memory(settings, data_dir=tmp_path), report, timelines, dry_run=True)
    restore_missing_deals(hs, settings, Memory(settings, data_dir=tmp_path), report, timelines, dry_run=False)
    assert hs.deals == []
    assert hs.contacts == []
    assert not any(w.get("action") == "restore" for w in report.proposed_writes if isinstance(w, dict))
    assert any("unknown_phone" in x for x in report.review_queue)


def test_myles_fuzzy_paid_client_is_notes_only(tmp_path):
    settings = make_settings()
    ev = _held(
        external_id="ff-myles2",
        first_name="Myles",
        last_name="MacAntosh",
        name="Myles MacAntosh",
        company="Emcor",
    )
    contact = _contact("c-emcor", "myles@emcor.com", "Myles", "McAntosh", "Emcor")
    deal = _deal("d-emcor", "c-emcor", STAGE["paid"], amount="8500", name="Myles McAntosh - Emcor")
    hs = FakeHubSpot([contact])
    hs.deals.append(deal)
    report = CycleReport()
    _handle_engagement(ev, settings, hs, Memory(settings, data_dir=tmp_path), None, report)
    assert hs.deals[0]["properties"]["dealstage"] == STAGE["paid"]
    assert not any(w[0] == "upsert_deal" for w in hs.writes)
    assert not report.deals_restored
