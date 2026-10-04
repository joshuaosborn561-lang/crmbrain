"""Hard-exclude not-deals, opener transcript guard, and body line casing."""

from datetime import datetime, timezone

from crmbrain.config import (
    RENEWAL_PIPELINE,
    STAGE,
    is_archived_hs_row,
    is_closed_won_client_domain,
    is_non_deal_person,
)
from crmbrain.models import Engagement
from crmbrain.nurture import (
    APPROVED_PROOF_LINES,
    CASE_STUDIES,
    GENERAL_PROOF,
    TRADES_PROOF,
    approved_proof_texts,
    capitalize_body_lines,
    collect_s2_hubspot,
    compose_nurture_draft,
    extract_spoken_want,
    infer_nurture_reason,
    is_not_deal_candidate,
    is_real_meeting_id,
    meeting_source_id,
    qualify_candidate,
    sample_hubspot_nurture_cards,
    spoken_clause_is_raw_transcript,
    summarize_spoken_want,
)
from crmbrain.policy import exclude_reason_for_nurture_or_deal, may_open_new_deal
from crmbrain.ticker import TickerCandidate
from tests.test_crm_gating import make_settings


HARD_EXCLUDE_NAMES = (
    "Cynthia Hernandez",
    "Alex Branning",
    "Jeremy Ciotola",
    "Bob Carlson",
    "Noah Brown",
    "Leroy Hite",
    "Gabriel Lopez",
    "Josh Bereano",
)


def test_hard_exclude_names_block_nurture_and_deal_sync():
    for name in HARD_EXCLUDE_NAMES:
        assert is_non_deal_person(name=name), name
        assert is_not_deal_candidate(name=name) == "non_deal", name
        ev = Engagement(
            source="calendly",
            external_id=name,
            name=name,
            first_name=name.split()[0],
            last_name=" ".join(name.split()[1:]),
            email=f"{name.split()[0].lower()}@example.com",
        )
        ok, reason = may_open_new_deal(ev, None, [])
        assert ok is False
        assert reason in {"not_deal", "non_deal"}

    assert is_non_deal_person(email="jeremy.ciotola@gmail.com")
    assert is_non_deal_person(email="noahbbrown951@gmail.com")
    assert is_not_deal_candidate(name="Jeremy Ciotola", email="jeremy.ciotola@gmail.com") == "non_deal"
    assert is_not_deal_candidate(name="Noah Brown", email="noahbbrown951@gmail.com") == "non_deal"
    assert is_not_deal_candidate(name="Kevin Hagemoser", email="kevin@kevinhagemoser.com") == "non_deal"
    assert not is_non_deal_person(name="Kevin Hagemoser", email="kevin@kevinhagemoser.com")
    assert not is_non_deal_person(name="Robert Lawson", email="rob@cyberguard360.com")


def test_archived_closed_won_domain_and_renewals_are_excluded():
    archived_contact = {"id": "c-arch", "archived": True, "properties": {"email": "pat@ok.test"}}
    assert is_archived_hs_row(archived_contact)
    assert (
        exclude_reason_for_nurture_or_deal(
            name="Pat Ok", email="pat@ok.test", contact=archived_contact
        )
        == "archived"
    )
    archived_deal = {"id": "d-arch", "archived": True, "properties": {"dealstage": STAGE["nurture"]}}
    assert (
        exclude_reason_for_nurture_or_deal(
            name="Pat Ok", email="pat@ok.test", deals=[archived_deal]
        )
        == "archived"
    )

    won = [{"id": "d-won", "properties": {"dealstage": STAGE["closed_won"], "pipeline": "default"}}]
    assert exclude_reason_for_nurture_or_deal(name="Dave Client", email="dave@won.test", deals=won) == "closed_won"
    assert is_closed_won_client_domain(email="ops@wonclient.com", extra_domains={"wonclient.com"})
    assert not is_closed_won_client_domain(email="ops@gmail.com", extra_domains={"gmail.com"})
    assert (
        exclude_reason_for_nurture_or_deal(
            name="Ops Person",
            email="ops@wonclient.com",
            extra={"closed_won_domains": {"wonclient.com"}},
        )
        == "closed_won"
    )

    renewal = [
        {
            "id": "d-ren",
            "properties": {"pipeline": RENEWAL_PIPELINE, "dealstage": "4391699185"},
        }
    ]
    assert exclude_reason_for_nurture_or_deal(name="Kyle Peterson", email="kyle@pete.test", deals=renewal) == "client"

    nurture = [
        {
            "id": "d-n",
            "dealstage": STAGE["nurture"],
            "archived": True,
            "contact": {"firstname": "Pat", "lastname": "Ok", "email": "pat@ok.test"},
            "last_activity": "2026-07-01T00:00:00+00:00",
            "source_note": {"body": "Source: roofing.", "created": "2026-07-01T00:00:00+00:00"},
        }
    ]
    assert collect_s2_hubspot(nurture, now=datetime(2026, 10, 2, tzinfo=timezone.utc)) == []

    jeremy = [
        {
            "id": "d-j",
            "dealstage": STAGE["nurture"],
            "contact": {"firstname": "Jeremy", "lastname": "Ciotola", "email": "jeremy.ciotola@gmail.com"},
            "last_activity": "2026-07-01T00:00:00+00:00",
            "source_note": {"body": "Source: intro.", "created": "2026-07-01T00:00:00+00:00"},
        }
    ]
    assert collect_s2_hubspot(jeremy, now=datetime(2026, 10, 2, tzinfo=timezone.utc)) == []

    cand = TickerCandidate(
        name="Noah Brown",
        email="noahbbrown951@gmail.com",
        reason="met",
        last_signal=datetime(2026, 7, 1, tzinfo=timezone.utc),
        source="hubspot",
        extra={"deal_stage": STAGE["nurture"], "booked": True, "met": True},
    )
    assert qualify_candidate(cand) == "non_deal"


def test_opener_rejects_raw_transcript_first_person_and_long_clause():
    raw = "Let's knock out a website. I will pick ONE offer and spend our focus on that for 90 days."
    assert spoken_clause_is_raw_transcript(raw)
    assert summarize_spoken_want(raw) == "you were focused on getting the website done first"
    draft = compose_nurture_draft(
        {
            "name": "Pat Reyes",
            "email": "pat@summitroofs.test",
            "company": "Summit Roofs",
            "reason": "met",
            "deal_stage": STAGE["nurture"],
            "meeting_at": "2026-09-10T18:00:00+00:00",
            "fireflies_id": "ff-pat-website",
            "meeting_source": "fireflies",
            "transcript": raw,
            "last_touch_snippet": raw,
        }
    )
    opener = draft.body.split("\n", 1)[0]
    assert "I will pick" not in opener
    assert "let's knock out" not in opener.lower()
    assert "you mentioned let's" not in opener.lower()
    assert "website" in opener.lower()
    assert "you were focused on getting the website done first" in opener.lower()
    assert len(opener.split()) <= 24
    assert draft.spoken_source_id == "ff-pat-website"

    long = " ".join(["pipeline"] * 21)
    assert spoken_clause_is_raw_transcript(long)
    fallback = compose_nurture_draft(
        {
            "name": "Pat Reyes",
            "reason": "met",
            "meeting_at": "2026-09-10T18:00:00+00:00",
            "last_touch_snippet": long,
        }
    )
    assert "following up on our" in fallback.body.split("\n", 1)[0].lower()
    assert "you mentioned" not in fallback.body.split("\n", 1)[0].lower()
    assert not fallback.spoken_source_id


def test_body_lines_start_with_a_capital_letter():
    raw = capitalize_body_lines("one of our roofers closed $100K\n\nworth a look?")
    assert raw.startswith("One of our roofers")
    assert "Worth a look?" in raw
    draft = compose_nurture_draft(
        {
            "name": "Jackie Darkazalli",
            "company": "Kelly Roofing",
            "industry": "roofing",
            "reason": "met",
            "last_touch_snippet": "Check back after our busy season.",
        }
    )
    for line in draft.body.splitlines():
        letters = [ch for ch in line if ch.isalpha()]
        if letters:
            assert letters[0].isupper(), line
    assert "One of our roofers closed $100K" in draft.body
    assert "one of our roofers" not in draft.body


class _FakeNurtureHS:
    def __init__(self):
        self.deals = {
            "nurture": [
                {
                    "id": "d-keep",
                    "properties": {
                        "dealname": "Jackie Darkazalli - Kelly Roofing",
                        "dealstage": STAGE["nurture"],
                        "pipeline": "default",
                        "createdate": "2026-04-10T20:30:00Z",
                        "description": "Check back after our busy season.",
                    },
                },
                {
                    "id": "d-jeremy",
                    "properties": {
                        "dealname": "Jeremy Ciotola",
                        "dealstage": STAGE["nurture"],
                        "pipeline": "default",
                        "createdate": "2026-07-24T18:00:00Z",
                    },
                },
                {
                    "id": "d-won-contact",
                    "properties": {
                        "dealname": "Dave Ackley - Goliath",
                        "dealstage": STAGE["nurture"],
                        "pipeline": "default",
                        "createdate": "2026-07-01T00:00:00Z",
                    },
                },
            ],
            "closedwon": [
                {
                    "id": "d-cw",
                    "properties": {"dealstage": STAGE["closed_won"], "pipeline": "default"},
                }
            ],
            "renewal": [],
        }
        self.contacts = {
            "d-keep": [
                {
                    "id": "c-jackie",
                    "properties": {
                        "firstname": "Jackie",
                        "lastname": "Darkazalli",
                        "email": "jackie@kellyroofing.com",
                        "company": "Kelly Roofing",
                        "personal_details": "Check back after our busy season.",
                    },
                }
            ],
            "d-jeremy": [
                {
                    "id": "c-jeremy",
                    "properties": {
                        "firstname": "Jeremy",
                        "lastname": "Ciotola",
                        "email": "jeremy.ciotola@gmail.com",
                    },
                }
            ],
            "d-won-contact": [
                {
                    "id": "c-dave",
                    "properties": {
                        "firstname": "Dave",
                        "lastname": "Ackley",
                        "email": "dave@goliath.com",
                        "company": "Goliath",
                    },
                }
            ],
            "d-cw": [
                {
                    "id": "c-dave",
                    "properties": {
                        "firstname": "Dave",
                        "lastname": "Ackley",
                        "email": "dave@goliath.com",
                    },
                }
            ],
        }

    def search_objects(self, object_name, filters, properties, max_results=400, page_limit=100):
        del object_name, properties, max_results, page_limit
        values = []
        for row in filters or []:
            if row.get("propertyName") == "dealstage" and row.get("value") == STAGE["nurture"]:
                return list(self.deals["nurture"])
            if row.get("propertyName") == "dealstage" and row.get("value") == STAGE["closed_won"]:
                return list(self.deals["closedwon"])
            if row.get("propertyName") == "pipeline" and row.get("value") == RENEWAL_PIPELINE:
                return list(self.deals["renewal"])
            values.append(row)
        return []

    def contacts_for_deal(self, deal_id):
        return list(self.contacts.get(str(deal_id), []))


class _ThreadGmail:
    def __init__(self):
        self.calls = []

    def find_contact_thread(self, email, name=""):
        self.calls.append((email, name))
        if email == "jackie@kellyroofing.com":
            return {
                "thread_id": "thread-jackie-live",
                "original_subject": "Follow up from our call earlier",
            }
        return None


def test_sample_cards_come_from_hubspot_nurture_and_skip_exclusions(tmp_path):
    settings = make_settings()
    out = tmp_path / "cards.json"
    payload = sample_hubspot_nurture_cards(
        settings, 10, hs=_FakeNurtureHS(), gmail=_ThreadGmail(), out_path=str(out)
    )
    names = [c["name"] for c in payload["cards"]]
    emails = [c["email"] for c in payload["cards"]]
    assert "Jackie Darkazalli" in names
    assert "jeremy.ciotola@gmail.com" not in emails
    assert "Jeremy Ciotola" not in names
    assert "dave@goliath.com" not in emails
    assert payload["source"] == "hubspot_nurture"
    assert payload["dealstage"] == STAGE["nurture"]
    assert out.is_file()
    jackie = next(c for c in payload["cards"] if c["name"] == "Jackie Darkazalli")
    assert jackie["body"].splitlines()[0][0].isupper()
    for line in jackie["body"].splitlines():
        letters = [ch for ch in line if ch.isalpha()]
        if letters:
            assert letters[0].isupper()
    assert jackie["reason"] in {"met", "booked", "kicked_can"}
    assert "Fit:" not in str(jackie["reason"])
    assert jackie["body"].split("\n\n")[1].rstrip().endswith(".")
    assert "Quick follow up" not in jackie["subject"]


class _CompanyIndustryHS(_FakeNurtureHS):
    def __init__(self):
        super().__init__()
        self.deals["nurture"].append(
            {
                "id": "d-rlp",
                "properties": {
                    "dealname": "Alex Rivera - RLP Mechanical",
                    "dealstage": STAGE["nurture"],
                    "pipeline": "default",
                    "createdate": "2026-04-12T15:00:00Z",
                },
            }
        )
        self.contacts["d-rlp"] = [
            {
                "id": "c-rlp",
                "properties": {
                    "firstname": "Alex",
                    "lastname": "Rivera",
                    "email": "alex@rlpmechanical.test",
                    "company": "RLP Mechanical",
                },
            }
        ]

    def associated_company_industry(self, *, contact_id="", deal_id=""):
        if contact_id == "c-rlp" or deal_id == "d-rlp":
            return "Mechanical or Industrial Engineering"
        return ""


def test_sample_cards_read_company_industry_and_name_keywords(tmp_path):
    settings = make_settings()
    payload = sample_hubspot_nurture_cards(
        settings, 10, hs=_CompanyIndustryHS(), out_path=str(tmp_path / "cards.json")
    )
    alex = next(c for c in payload["cards"] if c["email"] == "alex@rlpmechanical.test")
    assert "trades" in alex["body"].lower() or "$2M" in alex["body"]


class _CompanyHS(_FakeNurtureHS):
    def __init__(self):
        super().__init__()
        self.deals["nurture"].extend(
            [
                {
                    "id": "d-bradley",
                    "properties": {
                        "dealname": "Bradley Lord - Empire Roofing",
                        "dealstage": STAGE["nurture"],
                        "pipeline": "default",
                        "createdate": "2026-04-20T15:00:00Z",
                        "nurture_reason": "Fit: Empire Roofing wants to cut 80% of outbound grind.",
                    },
                },
                {
                    "id": "d-lionel",
                    "properties": {
                        "dealname": "Lionel Francis - Empire Roofing",
                        "dealstage": STAGE["nurture"],
                        "pipeline": "default",
                        "createdate": "2026-04-20T15:00:00Z",
                        "nurture_reason": "Fit: Empire Roofing wants to cut 80% of outbound grind.",
                    },
                },
                {
                    "id": "d-reese",
                    "properties": {
                        "dealname": "Reese Samala - The Roof Docs",
                        "dealstage": STAGE["nurture"],
                        "pipeline": "default",
                        "createdate": "2026-05-01T15:00:00Z",
                    },
                },
                {
                    "id": "d-ryan",
                    "properties": {
                        "dealname": "Ryan Parker - The Roof Docs",
                        "dealstage": STAGE["nurture"],
                        "pipeline": "default",
                        "createdate": "2026-05-02T15:00:00Z",
                    },
                },
                {
                    "id": "d-devx",
                    "properties": {
                        "dealname": "Pat Devx",
                        "dealstage": STAGE["nurture"],
                        "pipeline": "default",
                        "createdate": "2026-05-03T15:00:00Z",
                    },
                },
            ]
        )
        self.contacts.update(
            {
                "d-bradley": [
                    {
                        "id": "c-brad",
                        "properties": {
                            "firstname": "Bradley",
                            "lastname": "Lord",
                            "email": "bradley@empireroofing.com",
                            "company": "Empire Roofing",
                            "crm_source": "fireflies",
                        },
                    }
                ],
                "d-lionel": [
                    {
                        "id": "c-lionel",
                        "properties": {
                            "firstname": "Lionel",
                            "lastname": "Francis",
                            "email": "lionel@empireroofing.com",
                            "company": "Empire Roofing",
                        },
                    }
                ],
                "d-reese": [
                    {
                        "id": "c-reese",
                        "properties": {
                            "firstname": "Reese",
                            "lastname": "Samala",
                            "email": "reese@wrsroof.com",
                            "company": "wrsroof.com",
                        },
                    }
                ],
                "d-ryan": [
                    {
                        "id": "c-ryan",
                        "properties": {
                            "firstname": "Ryan",
                            "lastname": "Parker",
                            "email": "ryan@wrsroof.com",
                            "company": "wrsroof.com",
                        },
                    }
                ],
                "d-devx": [
                    {
                        "id": "c-devx",
                        "properties": {
                            "firstname": "Pat",
                            "lastname": "Devx",
                            "email": "pat@devx.com",
                            "company": "Devx",
                        },
                    }
                ],
            }
        )

    def last_meeting_at(self, contact_id):
        if contact_id == "c-jackie":
            return datetime(2026, 4, 10, 20, 30, tzinfo=timezone.utc)
        if contact_id == "c-brad":
            return datetime(2026, 4, 20, 15, 0, tzinfo=timezone.utc)
        return None


def test_sample_run_starts_new_threads_and_dedupes_company(tmp_path):
    settings = make_settings()
    gmail = _ThreadGmail()
    payload = sample_hubspot_nurture_cards(
        settings, 10, hs=_CompanyHS(), gmail=gmail, out_path=str(tmp_path / "cards.json")
    )
    assert gmail.calls == []
    jackie = next(c for c in payload["cards"] if c["email"] == "jackie@kellyroofing.com")
    assert jackie["thread_id"] == ""
    assert jackie["subject"] == "Kelly Roofing follow up"
    names = [c["name"] for c in payload["cards"]]
    empire = [n for n in names if n in {"Bradley Lord", "Lionel Francis"}]
    assert len(empire) == 1
    assert empire[0] == "Bradley Lord"
    roof_docs = [n for n in names if n in {"Reese Samala", "Ryan Parker"}]
    assert len(roof_docs) == 1
    assert payload["skipped"].get("same_company", 0) >= 2
    for card in payload["cards"]:
        assert "Fit:" not in str(card["reason"])
        assert card["reason"] in {"met", "booked", "kicked_can"}
        assert "wrsroof.com follow up" not in card["subject"].lower()
        assert "devx follow up" not in card["subject"].lower()
        assert card["subject"] != "Quick follow up"
        assert card["subject"] == "Following up" or card["subject"].endswith("follow up")
        if card["email"] == "pat@devx.com":
            assert card["subject"] == "Following up"
        proof = card["body"].split("\n\n")[1]
        assert proof.rstrip().endswith(".")


def test_reason_ignores_raw_hubspot_fit_note():
    from crmbrain.nurture import infer_nurture_reason

    note = "Fit: Empire Roofing wants to cut 80% of outbound grind."
    assert infer_nurture_reason(reason=note, deal_stage=STAGE["nurture"]) == "booked"
    assert infer_nurture_reason(reason=note, deal_stage=STAGE["nurture"], extra={"fireflies": True}) == "met"
    assert infer_nurture_reason(reason=note, extra={"booked": True}) == "booked"


def test_opener_uses_meeting_date_and_booked_never_met_does_not_claim_call():
    from crmbrain.nurture import compose_nurture_draft

    met = compose_nurture_draft(
        {
            "name": "Pat Reyes",
            "company": "Summit Roofs",
            "reason": "met",
            "meeting_at": "2026-09-10T18:00:00+00:00",
            "last_touch_snippet": "Fit: they want to knock out a website first.",
        }
    )
    opener = met.body.split("\n", 1)[0]
    assert "sep" in opener.lower()
    assert "fit:" not in opener.lower()
    assert "following up on our" in opener.lower()
    assert "website" not in opener.lower()

    booked = compose_nurture_draft(
        {
            "name": "Jonathan Matthews",
            "reason": "booked",
            "meeting_at": "2026-03-12T16:00:00+00:00",
            "last_touch_snippet": "",
        }
    )
    booked_opener = booked.body.split("\n", 1)[0].lower()
    assert "on our call" not in booked_opener
    assert "following up on our" not in booked_opener
    assert "circling back" in booked_opener
    assert "mar" in booked_opener

    undated = compose_nurture_draft(
        {
            "name": "Brian Donigan",
            "company": "Donigan",
            "reason": "met",
            "last_touch_snippet": "",
        }
    )
    undated_opener = undated.body.split("\n", 1)[0].lower()
    assert "wanted to circle back" in undated_opener
    assert "on our call" not in undated_opener
    assert "following up on our call" not in undated_opener


def test_proof_line_varies_by_vertical():
    from crmbrain.nurture import CASE_STUDIES, GENERAL_PROOF, TRADES_PROOF, compose_nurture_draft

    roof = compose_nurture_draft(
        {"name": "Pat Reyes", "company": "Kelly Roofing", "industry": "roofing"}
    )
    hvac = compose_nurture_draft(
        {"name": "Pat Reyes", "company": "The Chill Brothers", "industry": "hvac"}
    )
    staffing = compose_nurture_draft(
        {"name": "Pat Reyes", "company": "HireRight", "industry": "staffing"}
    )
    generic = compose_nurture_draft({"name": "Pat Reyes", "company": "Mystery Co"})
    assert roof.body.split("\n\n")[1].rstrip(".").lower() == CASE_STUDIES["roofing"].rstrip(".").lower()
    assert hvac.body.split("\n\n")[1].rstrip(".").lower() == TRADES_PROOF.rstrip(".").lower()
    assert staffing.body.split("\n\n")[1].rstrip(".").lower() == GENERAL_PROOF.rstrip(".").lower()
    assert generic.body.split("\n\n")[1].rstrip(".").lower() == GENERAL_PROOF.rstrip(".").lower()
    assert roof.body.split("\n\n")[1] != hvac.body.split("\n\n")[1]


def test_new_thread_subject_uses_title_case_company_not_domain():
    from crmbrain.nurture import compose_nurture_draft

    roof = compose_nurture_draft(
        {"name": "Reese Samala", "company": "the roof docs", "email": "reese@wrsroof.com"}
    )
    assert roof.subject == "The Roof Docs follow up"
    domain = compose_nurture_draft(
        {"name": "Pat Devx", "company": "Devx", "email": "pat@devx.com"}
    )
    assert domain.subject == "Following up"
    slug = compose_nurture_draft(
        {
            "name": "Chris",
            "company": "wtrenovations.com",
            "email": "chris@wtrenovations.com",
        }
    )
    assert slug.subject == "Following up"
    stored = compose_nurture_draft(
        {
            "name": "Jackie Darkazalli",
            "company": "Kelly Roofing",
            "nurture_thread_id": "th-nurture-1",
            "nurture_thread_subject": "Kelly Roofing follow up",
        }
    )
    assert stored.subject == "Re: Kelly Roofing follow up"


def test_first_send_stores_nurture_thread_and_later_reply_uses_it(tmp_path):
    from crmbrain.memory import Memory
    from crmbrain.nurture_actions import send_nurture_reply
    from tests.test_nurture_rebuild import FakeGmail, FakeSlack

    settings = make_settings(nurture_send_enabled=True)
    memory = Memory(settings, data_dir=tmp_path)
    memory._local["ticker"] = [
        {
            "id": "t-store",
            "name": "Pat Reyes",
            "email": "pat@summitroofs.test",
            "company": "Summit Roofs",
            "hs_contact_id": "c-pat",
            "hs_deal_id": "d-pat",
            "status": "active",
            "nurture_state": "queued",
        }
    ]
    gmail = FakeGmail()

    class _HS:
        def __init__(self):
            self.patches = []

        def patch_contact(self, cid, props, ev=None, contact=None):
            del ev, contact
            self.patches.append(("c", cid, dict(props)))

        def patch_deal(self, did, props):
            self.patches.append(("d", did, dict(props)))

    hs = _HS()
    first = send_nurture_reply(
        settings, memory, "t-store", gmail=gmail, hs=hs, slack=FakeSlack(), channel="C", ts="1"
    )
    assert first["ok"] is True
    assert first["thread_kind"] == "new_thread"
    assert gmail.sent[0]["threadId"] == ""
    row = memory.get_ticker("t-store")
    assert row["nurture_thread_id"] == "th-nurture-1"
    assert any(p[0] == "c" and p[2].get("nurture_thread_id") == "th-nurture-1" for p in hs.patches)
    memory.patch_ticker("t-store", {"nurture_state": "queued", "status": "active"})
    second = send_nurture_reply(
        settings, memory, "t-store", gmail=gmail, hs=hs, slack=FakeSlack(), channel="C", ts="2"
    )
    assert second["ok"] is True
    assert gmail.sent[1]["threadId"] == "th-nurture-1"
    assert gmail.sent[1]["subject"].lower().startswith("re:")


def test_every_proof_line_is_in_the_approved_set():
    from crmbrain.ticker import VERTICALS

    approved = approved_proof_texts()
    for raw in APPROVED_PROOF_LINES:
        assert raw.rstrip(".") in {line.rstrip(".") for line in approved}
    banned = (
        "one recruiting desk we support booked 40 qualified conversations in 90 days.",
        "home-services teams we work with are filling the calendar without adding another closer.",
        "an MSP we work with added $2M in pipeline last quarter without hiring another closer.",
        "a SaaS team we support booked a steady week of demos without standing up another SDR pod.",
        "advisor teams we support are booking 14+ conversations a month with people who already want to talk.",
        "an agency we support filled next month's calendar without adding another closer.",
    )
    for line in CASE_STUDIES.values():
        assert line.rstrip(".") in {p.rstrip(".") for p in APPROVED_PROOF_LINES}
    for industry in [row["key"] for row in VERTICALS] + ["staffing", "msp", "saas", "financial_advisors", "agency", ""]:
        draft = compose_nurture_draft({"name": "Pat Reyes", "company": "Acme", "industry": industry})
        proof = draft.body.split("\n\n")[1]
        assert proof in approved
        low = proof.lower()
        for bad in banned:
            assert bad.rstrip(".") not in low
    assert GENERAL_PROOF.rstrip(".") in {p.rstrip(".") for p in APPROVED_PROOF_LINES}
    assert TRADES_PROOF.rstrip(".") in {p.rstrip(".") for p in APPROVED_PROOF_LINES}


def test_kevin_hagemoser_is_nurture_excluded():
    assert is_not_deal_candidate(name="Kevin Hagemoser") == "non_deal"
    assert is_not_deal_candidate(email="kevin@kevinhagemoser.com") == "non_deal"
    cand = TickerCandidate(
        name="Kevin Hagemoser",
        email="kevin@kevinhagemoser.com",
        reason="met",
        last_signal=datetime(2026, 7, 1, tzinfo=timezone.utc),
        source="hubspot",
        extra={"deal_stage": STAGE["nurture"], "booked": True, "met": True},
    )
    assert qualify_candidate(cand) == "non_deal"
    kevin = [
        {
            "id": "d-kevin",
            "dealstage": STAGE["nurture"],
            "contact": {
                "firstname": "Kevin",
                "lastname": "Hagemoser",
                "email": "kevin@kevinhagemoser.com",
                "company": "Hagemoser",
            },
            "last_activity": "2026-07-01T00:00:00+00:00",
            "source_note": {"body": "Source: intro.", "created": "2026-07-01T00:00:00+00:00"},
        }
    ]
    assert collect_s2_hubspot(kevin, now=datetime(2026, 10, 2, tzinfo=timezone.utc)) == []


def test_focused_on_clause_requires_held_source_id():
    ungrounded = compose_nurture_draft(
        {
            "name": "Dan McGurl",
            "email": "dan@example.com",
            "reason": "met",
            "meeting_at": "2025-08-18T17:00:00+00:00",
            "last_touch_snippet": "Dan was focused on getting pricing nailed down.",
        }
    )
    opener = ungrounded.body.split("\n", 1)[0].lower()
    assert "pricing" not in opener
    assert "focused on" not in opener
    assert "following up on our" in opener
    assert not ungrounded.spoken_source_id

    grounded = compose_nurture_draft(
        {
            "name": "Dan McGurl",
            "email": "dan@example.com",
            "reason": "met",
            "meeting_at": "2025-08-18T17:00:00+00:00",
            "fireflies_id": "ff-dan-aug18",
            "meeting_source": "fireflies",
            "transcript": "Dan said he wants to get pricing nailed down before we send anything.",
        }
    )
    g_opener = grounded.body.split("\n", 1)[0].lower()
    assert "pricing" in g_opener
    assert "you were focused on getting pricing nailed down" in g_opener
    assert grounded.spoken_source_id == "ff-dan-aug18"


def test_booked_opener_upgrades_to_held_when_fireflies_is_near_date():
    hamdat = compose_nurture_draft(
        {
            "name": "Hamdat Wahid",
            "email": "hamdat@example.com",
            "reason": "booked",
            "createdate": "2026-09-03T12:00:00+00:00",
            "meeting_at": "2026-09-04T15:00:00+00:00",
            "fireflies_id": "ff-hamdat-sep4",
            "meeting_source": "fireflies",
            "held_at": "2026-09-04T15:20:00+00:00",
            "meeting_date_source": "fireflies",
            "extra": {
                "fireflies": True,
                "fireflies_id": "ff-hamdat-sep4",
                "held_at": "2026-09-04T15:20:00+00:00",
                "booked": True,
                "createdate": "2026-09-03T12:00:00+00:00",
                "meeting_date_source": "fireflies",
            },
        }
    )
    opener = hamdat.body.split("\n", 1)[0].lower()
    assert "meeting we had booked" not in opener
    assert "following up on our" in opener
    assert "sep" in opener
    assert hamdat.meeting_source_id == "ff-hamdat-sep4"

    still_booked = compose_nurture_draft(
        {
            "name": "Jonathan Matthews",
            "reason": "booked",
            "meeting_at": "2026-03-12T16:00:00+00:00",
        }
    )
    booked_opener = still_booked.body.split("\n", 1)[0].lower()
    assert "meeting we had booked" in booked_opener
    assert "mar" in booked_opener


DAN_FF_ID = "01M08PYCZF2GVS80MNPQKH8ZPF"
DAN_MEETING_SUMMARY = (
    "401k lead gen for financial advisors. Dual campaign covering existing 401k plans "
    "plus startups. Dan will take the proposal to his advisor team and leadership. "
    "We briefly mentioned pricing at the end."
)
DAN_GENERIC_NOTES = "Dan was focused on getting pricing nailed down."


def test_real_meeting_id_rejects_source_and_date_fallbacks():
    assert is_real_meeting_id(DAN_FF_ID)
    assert is_real_meeting_id("ff-dan-aug18")
    assert is_real_meeting_id("cube-drivefileidxx")
    assert not is_real_meeting_id("fireflies")
    assert not is_real_meeting_id("fireflies:2026-08-18")
    assert not is_real_meeting_id("cube_acr")
    assert not is_real_meeting_id("")


def test_dan_mcgurl_fixture_does_not_invent_pricing_from_notes():
    notes_only = compose_nurture_draft(
        {
            "name": "Dan McGurl",
            "email": "dan@example.com",
            "reason": "met",
            "meeting_at": "2026-08-18T17:00:00+00:00",
            "meeting_source": "fireflies",
            "fireflies": True,
            "crm_source": "fireflies",
            "last_touch_snippet": DAN_GENERIC_NOTES,
            "description": DAN_GENERIC_NOTES,
            "nurture_reason": DAN_GENERIC_NOTES,
            "personal_details": DAN_GENERIC_NOTES,
            "extra": {
                "fireflies": True,
                "meeting_source": "fireflies",
                "crm_source": "fireflies",
                "last_touch_snippet": DAN_GENERIC_NOTES,
            },
        }
    )
    notes_opener = notes_only.body.split("\n", 1)[0].lower()
    assert "pricing" not in notes_opener
    assert "focused on" not in notes_opener
    assert "following up on our aug 18 call" in notes_opener
    assert not notes_only.spoken_source_id
    assert not notes_only.meeting_source_id
    assert meeting_source_id(
        {
            "meeting_source": "fireflies",
            "fireflies": True,
            "meeting_at": "2026-08-18T17:00:00+00:00",
            "source_id": "fireflies:2026-08-18",
        }
    ) == ""

    grounded = compose_nurture_draft(
        {
            "name": "Dan McGurl",
            "email": "dan@example.com",
            "reason": "met",
            "meeting_at": "2026-08-18T17:00:00+00:00",
            "fireflies_id": DAN_FF_ID,
            "meeting_source": "fireflies",
            "summary": DAN_MEETING_SUMMARY,
            "action_items": [
                "Dan to take the proposal to his advisor team and leadership.",
            ],
            "last_touch_snippet": DAN_GENERIC_NOTES,
            "description": DAN_GENERIC_NOTES,
        }
    )
    opener = grounded.body.split("\n", 1)[0].lower()
    assert "pricing" not in opener
    assert "focused on" not in opener
    assert "proposal" not in opener
    assert "following up on our aug 18 call" in opener
    assert not grounded.spoken_source_id
    assert grounded.meeting_source_id == DAN_FF_ID
    clause, confidence = extract_spoken_want(DAN_MEETING_SUMMARY)
    assert clause == ""
    assert confidence < 0.7
    assert summarize_spoken_want(DAN_MEETING_SUMMARY) == ""


def test_brian_donigan_source_includes_its_date():
    undated = compose_nurture_draft(
        {
            "name": "Brian Donigan",
            "company": "Donigan",
            "reason": "met",
            "meeting_source": "fireflies",
            "fireflies": True,
            "source_id": "fireflies",
            "last_touch_snippet": "",
        }
    )
    undated_opener = undated.body.split("\n", 1)[0].lower()
    assert "wanted to circle back" in undated_opener
    assert not undated.meeting_source_id
    assert undated.meeting_source_id != "fireflies"

    dated = compose_nurture_draft(
        {
            "name": "Brian Donigan",
            "company": "Donigan",
            "reason": "met",
            "fireflies_id": "01BRIANDONIGANMEETIDXX",
            "meeting_source": "fireflies",
            "signal_at": "2026-07-15T16:00:00+00:00",
            "held_at": "2026-07-15T16:00:00+00:00",
            "extra": {
                "fireflies": True,
                "fireflies_id": "01BRIANDONIGANMEETIDXX",
                "signal_at": "2026-07-15T16:00:00+00:00",
                "held_at": "2026-07-15T16:00:00+00:00",
            },
        }
    )
    dated_opener = dated.body.split("\n", 1)[0].lower()
    assert "following up on our jul 15 call" in dated_opener
    assert dated.meeting_source_id == "01BRIANDONIGANMEETIDXX"
    assert dated.meeting_source_id != "fireflies"


JO_GMAIL_MESSAGES = [
    {
        "id": "m-jo-intro",
        "subject": "New Event: Jo Fried - SalesGlider Intro",
        "body": "Event Type: SalesGlider Intro\nInvitee: Jo Fried\nEvent Date/Time: 10:00am - Friday, March 27, 2026 (CDT)",
        "from": "noreply@calendly.com",
    },
    {
        "id": "m-jo-followup",
        "subject": "Accepted: SalesGlider Followup @ Wed Apr 1, 2026 10am - 10:30am (CDT)",
        "body": "When: Wednesday Apr 1, 2026 10:00am (Central Time - Chicago)",
        "from": "calendar-notification@google.com",
        "accepted": True,
    },
    {
        "id": "m-jo-deck",
        "subject": "IntegriBuilt Growth Playbook",
        "body": "Deck from our call.",
        "from": "Joshua Osborn <joshua@salesglidergrowth.com>",
        "date": "2026-04-01T18:00:00+00:00",
    },
]

JOSH_BROWN_GMAIL_MESSAGES = [
    {
        "id": "m-jb-book",
        "subject": "New Event: Josh Brown - SalesGlider Web Booking",
        "body": "Event Type: SalesGlider Web Booking\nInvitee: Josh Brown\nEvent Date/Time: 10:00am - Monday, March 30, 2026 (CDT)",
        "from": "noreply@calendly.com",
    },
]

BRIAN_GMAIL_MESSAGES = [
    {
        "id": "m-brian-book",
        "subject": "Accepted: SalesGlider Intro @ Fri Sep 25, 2026 2:00pm - 2:30pm (CDT)",
        "body": "When: Friday Sep 25, 2026 2:00pm (Central Time - Chicago)",
        "from": "calendar-notification@google.com",
        "accepted": True,
    },
]


def test_parse_invite_when_from_gcal_and_calendly():
    from crmbrain.sources.gmail_scan import parse_meeting_at

    followup = parse_meeting_at(
        "Accepted: SalesGlider Followup @ Wed Apr 1, 2026 10am - 10:30am (CDT)",
        "When: Wednesday Apr 1, 2026 10:00am (Central Time - Chicago)",
    )
    assert followup is not None
    assert followup.month == 4 and followup.day == 1
    booking = parse_meeting_at(
        "New Event: Josh Brown - SalesGlider Web Booking",
        "Event Date/Time: 10:00am - Monday, March 30, 2026 (CDT)",
    )
    assert booking is not None
    assert booking.month == 3 and booking.day == 30
    brian = parse_meeting_at(
        "Accepted: SalesGlider Intro @ Fri Sep 25, 2026 2:00pm - 2:30pm (CDT)", ""
    )
    assert brian is not None
    assert brian.month == 9 and brian.day == 25


def test_jo_fried_gmail_followup_is_held_apr_1():
    row = {
        "name": "Jo Fried",
        "email": "jo@integribuilt.test",
        "company": "IntegriBuilt",
        "reason": "booked",
        "deal_stage": STAGE["nurture"],
        "createdate": "2026-09-03T12:00:00+00:00",
        "meeting_at": "2026-09-03T12:00:00+00:00",
        "signal_at": "2026-09-03T12:00:00+00:00",
        "hs_lastmodifieddate": "2026-09-03T12:05:00+00:00",
        "gmail_messages": JO_GMAIL_MESSAGES,
        "extra": {
            "createdate": "2026-09-03T12:00:00+00:00",
            "gmail_messages": JO_GMAIL_MESSAGES,
            "deal_stage": STAGE["nurture"],
        },
    }
    assert infer_nurture_reason(reason="booked", deal_stage=STAGE["nurture"], extra=row) == "met"
    draft = compose_nurture_draft(row)
    opener = draft.body.split("\n", 1)[0].lower()
    assert "following up on our apr 1 call" in opener
    assert "sep" not in opener
    assert "mar 27" not in opener
    assert "meeting we had booked" not in opener


def test_josh_brown_uses_web_booking_mar_30_not_createdate():
    row = {
        "name": "Josh Brown",
        "email": "josh@ampmediasales.test",
        "company": "AMP Media Sales",
        "reason": "booked",
        "deal_stage": STAGE["nurture"],
        "createdate": "2026-09-03T12:00:00+00:00",
        "meeting_at": "2026-09-03T12:00:00+00:00",
        "signal_at": "2026-09-03T12:00:00+00:00",
        "gmail_messages": JOSH_BROWN_GMAIL_MESSAGES,
        "extra": {
            "createdate": "2026-09-03T12:00:00+00:00",
            "gmail_messages": JOSH_BROWN_GMAIL_MESSAGES,
        },
    }
    assert infer_nurture_reason(reason="booked", deal_stage=STAGE["nurture"], extra=row) == "booked"
    draft = compose_nurture_draft(row)
    opener = draft.body.split("\n", 1)[0].lower()
    assert "circling back on the mar 30 meeting we had booked" in opener
    assert "sep" not in opener


def test_undated_booked_uses_call_we_had_set_up():
    draft = compose_nurture_draft(
        {
            "name": "Jo Fried",
            "company": "IntegriBuilt",
            "reason": "booked",
            "createdate": "2026-09-03T12:00:00+00:00",
            "meeting_at": "2026-09-03T12:00:00+00:00",
            "signal_at": "2026-09-03T12:00:00+00:00",
        }
    )
    opener = draft.body.split("\n", 1)[0].lower()
    assert opener == "hey jo, circling back on the call we had set up."
    assert "sep" not in opener
    assert "meeting we had booked" not in opener


def test_shaun_and_hamdat_dates_need_real_booking_events():
    shaun_real = compose_nurture_draft(
        {
            "name": "Shaun Rodriquez",
            "reason": "booked",
            "createdate": "2026-09-03T12:00:00+00:00",
            "gmail_messages": [
                {
                    "subject": "New Event: Shaun Rodriquez - SalesGlider Web Booking",
                    "body": "Monday, September 23, 2026 at 10:00 AM",
                    "from": "noreply@calendly.com",
                }
            ],
        }
    )
    assert "sep 23" in shaun_real.body.split("\n", 1)[0].lower()

    shaun_createdate = compose_nurture_draft(
        {
            "name": "Shaun Rodriquez",
            "reason": "booked",
            "createdate": "2026-09-23T12:00:00+00:00",
            "meeting_at": "2026-09-23T12:00:00+00:00",
            "signal_at": "2026-09-23T12:00:00+00:00",
        }
    )
    shaun_opener = shaun_createdate.body.split("\n", 1)[0].lower()
    assert "sep" not in shaun_opener
    assert "call we had set up" in shaun_opener

    hamdat = compose_nurture_draft(
        {
            "name": "Hamdat Wahid",
            "reason": "booked",
            "createdate": "2026-09-03T12:00:00+00:00",
            "meeting_at": "2026-09-04T15:00:00+00:00",
            "fireflies_id": "ff-hamdat-sep4",
            "meeting_source": "fireflies",
            "held_at": "2026-09-04T15:20:00+00:00",
            "meeting_date_source": "fireflies",
        }
    )
    assert "sep 4" in hamdat.body.split("\n", 1)[0].lower()
    assert "following up on our" in hamdat.body.split("\n", 1)[0].lower()


class _JoJoshHS:
    def __init__(self):
        self.deals = {
            "nurture": [
                {
                    "id": "d-jo",
                    "properties": {
                        "dealname": "Jo Fried - IntegriBuilt",
                        "dealstage": STAGE["nurture"],
                        "pipeline": "default",
                        "createdate": "2026-09-03T12:00:00Z",
                    },
                },
                {
                    "id": "d-jb",
                    "properties": {
                        "dealname": "Josh Brown - AMP Media Sales",
                        "dealstage": STAGE["nurture"],
                        "pipeline": "default",
                        "createdate": "2026-09-03T12:00:00Z",
                    },
                },
                {
                    "id": "d-brian",
                    "properties": {
                        "dealname": "Brian Donigan - Donigan",
                        "dealstage": STAGE["nurture"],
                        "pipeline": "default",
                        "createdate": "2026-09-03T12:00:00Z",
                    },
                },
            ],
            "closedwon": [],
            "renewal": [],
        }
        self.contacts = {
            "d-jo": [
                {
                    "id": "c-jo",
                    "properties": {
                        "firstname": "Jo",
                        "lastname": "Fried",
                        "email": "jo@integribuilt.test",
                        "company": "IntegriBuilt",
                    },
                }
            ],
            "d-jb": [
                {
                    "id": "c-jb",
                    "properties": {
                        "firstname": "Josh",
                        "lastname": "Brown",
                        "email": "josh@ampmediasales.test",
                        "company": "AMP Media Sales",
                    },
                }
            ],
            "d-brian": [
                {
                    "id": "c-brian",
                    "properties": {
                        "firstname": "Brian",
                        "lastname": "Donigan",
                        "email": "brian@donigan.test",
                        "company": "Donigan",
                        "crm_source": "fireflies",
                    },
                }
            ],
        }

    def search_objects(self, object_name, filters, properties, max_results=400, page_limit=100):
        del object_name, properties, max_results, page_limit
        for row in filters or []:
            if row.get("propertyName") == "dealstage" and row.get("value") == STAGE["nurture"]:
                return list(self.deals["nurture"])
            if row.get("propertyName") == "dealstage" and row.get("value") == STAGE["closed_won"]:
                return list(self.deals["closedwon"])
            if row.get("propertyName") == "pipeline" and row.get("value") == RENEWAL_PIPELINE:
                return list(self.deals["renewal"])
        return []

    def contacts_for_deal(self, deal_id):
        return list(self.contacts.get(str(deal_id), []))


class _MeetingGmail:
    """Real Gmail list() shape: ids only. Bodies come from get()."""

    def __init__(self, settings=None):
        del settings
        self._by_id = {
            row["id"]: row
            for row in JO_GMAIL_MESSAGES + JOSH_BROWN_GMAIL_MESSAGES + BRIAN_GMAIL_MESSAGES
        }

    def search(self, query, max_results=15):
        del max_results
        q = (query or "").lower()
        if "jo@integribuilt.test" in q:
            return [{"id": row["id"]} for row in JO_GMAIL_MESSAGES]
        if "josh@ampmediasales.test" in q:
            return [{"id": row["id"]} for row in JOSH_BROWN_GMAIL_MESSAGES]
        if "brian@donigan.test" in q:
            return [{"id": row["id"]} for row in BRIAN_GMAIL_MESSAGES]
        return []

    def get(self, message_id):
        row = self._by_id[message_id]
        return {
            "id": message_id,
            "snippet": row.get("body") or "",
            "payload": {
                "headers": [
                    {"name": "Subject", "value": row.get("subject") or ""},
                    {"name": "From", "value": row.get("from") or ""},
                ],
                "mimeType": "text/plain",
                "body": {},
            },
        }

    def headers_map(self, message):
        return {
            item["name"].lower(): item.get("value", "")
            for item in (message.get("payload") or {}).get("headers") or []
        }

    def body_text(self, message):
        mid = str(message.get("id") or "")
        return str((self._by_id.get(mid) or {}).get("body") or message.get("snippet") or "")

    def calendar_parts(self, message):
        del message
        return []


def test_sample_cards_use_gmail_booking_and_followup_dates(tmp_path):
    settings = make_settings()
    payload = sample_hubspot_nurture_cards(
        settings, 10, hs=_JoJoshHS(), gmail=_MeetingGmail(), out_path=str(tmp_path / "cards.json")
    )
    jo = next(c for c in payload["cards"] if c["name"] == "Jo Fried")
    assert jo["reason"] == "met"
    assert "following up on our apr 1 call" in jo["body"].split("\n", 1)[0].lower()
    assert "sep" not in jo["body"].split("\n", 1)[0].lower()
    josh = next(c for c in payload["cards"] if c["name"] == "Josh Brown")
    assert josh["reason"] == "booked"
    assert "mar 30" in josh["body"].split("\n", 1)[0].lower()
    assert "sep" not in josh["body"].split("\n", 1)[0].lower()
    brian = next(c for c in payload["cards"] if c["name"] == "Brian Donigan")
    assert "sep 25" in brian["body"].split("\n", 1)[0].lower()
    assert "sep 3" not in brian["body"].split("\n", 1)[0].lower()


def test_sample_cards_builds_gmail_client_when_omitted(tmp_path, monkeypatch):
    created = {}

    class _BuiltGmail(_MeetingGmail):
        def __init__(self, settings=None):
            created["yes"] = True
            super().__init__(settings)

    monkeypatch.setattr("crmbrain.gmail_client.Gmail", _BuiltGmail)
    settings = make_settings(
        gmail_refresh_token="refresh-token",
        gmail_client_id="id",
        gmail_client_secret="secret",
    )
    payload = sample_hubspot_nurture_cards(
        settings, 10, hs=_JoJoshHS(), out_path=str(tmp_path / "cards.json")
    )
    assert created.get("yes") is True
    jo = next(c for c in payload["cards"] if c["name"] == "Jo Fried")
    assert jo["reason"] == "met"
    assert "apr 1" in jo["body"].split("\n", 1)[0].lower()
