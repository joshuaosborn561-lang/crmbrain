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
    capitalize_body_lines,
    collect_s2_hubspot,
    compose_nurture_draft,
    is_not_deal_candidate,
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
            "name": "Kevin Hagemoser",
            "email": "kevin@kevinhagemoser.com",
            "company": "Kevin Hagemoser",
            "reason": "met",
            "deal_stage": STAGE["nurture"],
            "meeting_at": "2026-09-10T18:00:00+00:00",
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

    long = " ".join(["pipeline"] * 21)
    assert spoken_clause_is_raw_transcript(long)
    fallback = compose_nurture_draft(
        {
            "name": "Kevin Hagemoser",
            "reason": "met",
            "meeting_at": "2026-09-10T18:00:00+00:00",
            "last_touch_snippet": long,
        }
    )
    assert "following up on our" in fallback.body.split("\n", 1)[0].lower()
    assert "you mentioned" not in fallback.body.split("\n", 1)[0].lower()


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


def test_sample_cards_come_from_hubspot_nurture_and_skip_exclusions(tmp_path):
    settings = make_settings()
    out = tmp_path / "cards.json"
    payload = sample_hubspot_nurture_cards(
        settings, 10, hs=_FakeNurtureHS(), out_path=str(out)
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
