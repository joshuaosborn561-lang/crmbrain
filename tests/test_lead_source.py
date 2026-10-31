from datetime import datetime, timezone

import pytest

from crmbrain import lead_source as ls
from crmbrain.deal_write import commit_deal_write
from crmbrain.hubspot import CONTACT_PROPS, DEAL_PROPS, HubSpot
from crmbrain.lead_source import LeadSourceContext, classify_campaign, resolve_lead_source
from crmbrain.models import Engagement
from crmbrain.sources import smartlead


class _Mem:
    use_supabase = True

    def __init__(self, tables=None, fail=False):
        self.tables = tables or {}
        self.fail = fail
        self.calls = []

    def _sb_named(self, schema, method, table, params=None, **kw):
        self.calls.append((schema, table, params))
        if self.fail:
            raise RuntimeError("down")
        return self.tables.get(f"{schema}.{table}", self.tables.get(table, []))


class _Settings:
    smartlead_key = "k"


class _Gmail:
    def __init__(self, messages):
        self.messages = messages

    def search(self, query, max_results=50):
        return [{"id": m["id"]} for m in self.messages]

    def get(self, mid):
        return next(m for m in self.messages if m["id"] == mid)

    def headers_map(self, m):
        return m["headers"]

    def body_text(self, m):
        return m.get("body", "")


def _msg(mid, ts, frm, to, cc="", subject="Hello", body=""):
    return {
        "id": mid,
        "internalDate": str(ts),
        "headers": {"from": frm, "to": to, "cc": cc, "subject": subject},
        "body": body,
    }


def _ev(source="calendly", email="pat@prospect.com", **extra):
    return Engagement(source=source, external_id="1", email=email, first_name="Pat", last_name="Prospect", extra=extra)


def _ctx(monkeypatch, *, tables=None, campaigns=None, gmail=None, fail_sl=False, memory=None):
    def fake_lookup(settings, email):
        if fail_sl:
            raise RuntimeError("429")
        return campaigns or []

    monkeypatch.setattr(smartlead, "lead_campaigns", fake_lookup)
    return LeadSourceContext(settings=_Settings(), memory=memory or _Mem(tables), gmail=gmail)


# ---- properties ----------------------------------------------------------------------------------


def test_property_definitions_cover_all_sources():
    for props in (CONTACT_PROPS, DEAL_PROPS):
        by = {p["name"]: p for p in props}
        assert [o["value"] for o in by["sg_lead_source"]["options"]] == list(ls.LEAD_SOURCES)
        assert [o["value"] for o in by["sg_lead_source_confidence"]["options"]] == ["confirmed", "inferred"]


# ---- campaign names (real Smartlead names) -------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("LiveLeads | Staffing Owners | 2026-10", ("Live leads", None)),
        ("LiveLeads | Competitor Belkins | 2026-10", ("Live leads", None)),
        ("SG Influencer Partnerships 1007", ("Partner play", None)),
        ("SG Referral Hosts 1015 - IT", ("Referral", None)),
        ("SG Referral Hosts - Faith - 2026-10-26", ("Referral", None)),
        ("Cold Call Followup", ("Cold call", None)),
        ("SalesGlider Warm Web Visitors", ("Inbound/other", "inferred")),
        ("RB2B Web Intent", ("Inbound/other", "inferred")),
        ("SalesGlider Staffing", ("Cold email", None)),
        ("SG Specialty Owners Commercial 0922", ("Cold email", None)),
        ("MSPs Referrals RTS Lookalike", ("Cold email", None)),
        ("Insight Partner Sucks", ("Cold email", None)),
    ],
)
def test_classify_campaign(name, expected):
    assert classify_campaign(name) == expected


# ---- engagement-level channels -------------------------------------------------------------------


def test_direct_channels_are_confirmed():
    assert resolve_lead_source(_ev("heyreach")) == ("LinkedIn/HeyReach", "confirmed")
    assert resolve_lead_source(_ev("allo")) == ("Cold call", "confirmed")
    assert resolve_lead_source(_ev("rvm")) == ("Cold call", "confirmed")


def test_smartlead_engagement_uses_campaign_name():
    assert resolve_lead_source(_ev("smartlead", campaign_name="SalesGlider Staffing")) == ("Cold email", "confirmed")
    assert resolve_lead_source(_ev("smartlead", campaign_name="LiveLeads | MSP-IT Owners | 2026-10")) == ("Live leads", "confirmed")
    assert resolve_lead_source(_ev("smartlead", campaign_name="SG Influencer Partnerships 1007")) == ("Partner play", "confirmed")
    assert resolve_lead_source(_ev("smartlead", campaign_name="SalesGlider Warm Web Visitors")) == ("Inbound/other", "inferred")
    assert resolve_lead_source(_ev("smartlead", campaign_name="Bolder - C1", client_campaign=True)) == ("Cold email", "inferred")


# ---- Referral via Gmail --------------------------------------------------------------------------


def test_referral_known_account_on_first_thread_is_confirmed(monkeypatch):
    gm = _Gmail([
        _msg("2", 2000, "pat@prospect.com", "joshua@salesglidergrowth.com", subject="Re: Sales intro"),
        _msg("1", 1000, "TJ <tj@culture-fits.com>", "pat@prospect.com, joshua@salesglidergrowth.com", subject="Chat"),
    ])
    assert resolve_lead_source(_ev(), _ctx(monkeypatch, gmail=gm)) == ("Referral", "confirmed")


def test_referral_intro_wording_from_unknown_third_party_is_confirmed(monkeypatch):
    gm = _Gmail([
        _msg("1", 1000, "Sam <sam@someco.com>", "pat@prospect.com", cc="joshua@salesglidergrowth.com",
             subject="Introduction: Pat <> Josh", body="Wanted to introduce you two."),
    ])
    assert resolve_lead_source(_ev(), _ctx(monkeypatch, gmail=gm)) == ("Referral", "confirmed")


def test_prospect_only_cc_d_on_someone_elses_intro_is_not_referral(monkeypatch):
    # Real shape: Dan McGurl (Savvy) introduced Josh to Aaron; Dan is cc'd, so Dan is the introducer.
    gm = _Gmail([
        _msg("1", 1000, "Aaron <apeloquin@p6wa.com>", "joshua@salesglidergrowth.com", cc="Pat <pat@prospect.com>",
             subject="Re: Introducing Joshua to Aaron - 401(k) Lead Prospecting"),
    ])
    assert resolve_lead_source(_ev(), _ctx(monkeypatch, gmail=gm)) is None or \
        resolve_lead_source(_ev(), _ctx(monkeypatch, gmail=gm))[0] != "Referral"


def test_intro_wording_without_prospect_name_in_subject_is_inferred(monkeypatch):
    gm = _Gmail([_msg("1", 1000, "Sam <sam@someco.com>", "pat@prospect.com", cc="joshua@salesglidergrowth.com",
                      subject="Quick intro", body="Wanted to introduce you two.")])
    assert resolve_lead_source(_ev(), _ctx(monkeypatch, gmail=gm)) == ("Referral", "inferred")


def test_forwarded_intro_from_known_account_naming_prospect_is_confirmed(monkeypatch):
    # TJ emails Josh alone ("Sales intro") about the prospect; the prospect is not on the thread.
    class G(_Gmail):
        def search(self, query, max_results=50):
            return [{"id": "9"}] if "intro" in query else []

    gm = G([_msg("9", 1000, "Terrell Jackson <TJ@culture-fits.com>", "joshua@salesglidergrowth.com",
                 subject="Sales intro", body="Meet Pat Prospect at prospect.com, great fit.")])
    assert resolve_lead_source(_ev(), _ctx(monkeypatch, gmail=gm)) == ("Referral", "confirmed")


def test_oldest_messages_come_from_the_tail_of_the_page(monkeypatch):
    class G(_Gmail):
        def search(self, query, max_results=50):
            assert max_results == 100
            return [{"id": str(i)} for i in range(20, 0, -1)]  # newest first

        def get(self, mid):
            return _msg(mid, int(mid) * 1000, "tj@culture-fits.com" if mid == "1" else "pat@prospect.com",
                        "joshua@salesglidergrowth.com", cc="pat@prospect.com" if mid == "1" else "")

    out = resolve_lead_source(_ev(), _ctx(monkeypatch, gmail=G([])))
    assert out == ("Referral", "confirmed")


def test_prospect_writing_to_a_third_party_or_introducing_josh_is_not_referral(monkeypatch):
    # Real shapes: Nutter emailing his own contact "Following our call re: ...", Dan McGurl introducing Josh to Aaron.
    class G(_Gmail):
        def search(self, query, max_results=50):
            return [{"id": "1"}] if "intro" in query else []

    for frm, to, subj in [
        ("Pat <pat@prospect.com>", "Edwin <ejames@pcswga.org>", "Re: Following our call re: Prospect recruitment services"),
        ("Pat <pat@prospect.com>", "joshua@salesglidergrowth.com, Aaron <apeloquin@p6wa.com>", "Introducing Joshua to Aaron"),
    ]:
        gm = G([_msg("1", 1000, frm, to, subject=subj, body="introduce")])
        out = resolve_lead_source(_ev(), _ctx(monkeypatch, gmail=gm))
        assert out is None or out[0] != "Referral"


def test_referral_cc_without_intro_wording_is_inferred(monkeypatch):
    gm = _Gmail([
        _msg("1", 1000, "joshua@salesglidergrowth.com", "pat@prospect.com", cc="Lee <lee@otherco.com>", subject="Following up"),
    ])
    assert resolve_lead_source(_ev(), _ctx(monkeypatch, gmail=gm)) == ("Referral", "inferred")


def test_gmail_with_only_josh_and_prospect_is_not_referral(monkeypatch):
    gm = _Gmail([_msg("1", 1000, "pat@prospect.com", "joshua@salesglidergrowth.com"),
                 _msg("2", 1500, "calendar-notification@google.com", "pat@prospect.com")])
    out = resolve_lead_source(_ev(), _ctx(monkeypatch, gmail=gm))
    assert out != ("Referral", "confirmed") and (out is None or out[0] != "Referral")


def test_referral_beats_a_cold_email_reply(monkeypatch):
    gm = _Gmail([_msg("1", 1000, "tj@culture-fits.com", "pat@prospect.com", subject="Sales intro")])
    camps = [{"campaign_name": "SalesGlider Staffing", "last_reply_at": "2026-10-01T00:00:00Z"}]
    assert resolve_lead_source(_ev(), _ctx(monkeypatch, campaigns=camps, gmail=gm)) == ("Referral", "confirmed")


def test_referral_accounts_env_extends(monkeypatch):
    monkeypatch.setenv("REFERRAL_ACCOUNTS", "friend.org")
    gm = _Gmail([_msg("1", 1000, "a@friend.org", "pat@prospect.com", subject="hi")])
    assert resolve_lead_source(_ev(), _ctx(monkeypatch, gmail=gm)) == ("Referral", "confirmed")


# ---- Cold call via Allo --------------------------------------------------------------------------


def test_allo_call_to_phone_is_confirmed_and_beats_email_reply(monkeypatch):
    ev = _ev()
    ev.phone = "+1 (973) 555-0142"
    camps = [{"campaign_name": "SalesGlider Staffing", "last_reply_at": "2026-08-03T00:00:00Z"}]
    ctx = _ctx(monkeypatch, tables={"allo.calls": [{"id": "c1"}]}, campaigns=camps)
    assert resolve_lead_source(ev, ctx) == ("Cold call", "confirmed")
    sel = [c for c in ctx.memory.calls if c[1] == "calls"][0][2]
    assert sel["contact_number"] == "ilike.*9735550142"


def test_allo_name_only_match_is_inferred(monkeypatch):
    mem = _Mem()
    ctx = _ctx(monkeypatch, memory=mem)
    seen = []

    def sb(schema, method, table, params=None, **kw):
        seen.append(params)
        return [{"id": "c1"}] if params and "contact_name" in params else []

    mem._sb_named = sb
    assert resolve_lead_source(_ev(), ctx) == ("Cold call", "inferred")


# ---- LinkedIn / Cold email / Live leads / Partner play via lookup --------------------------------


def test_heyreach_ticker_row_is_confirmed(monkeypatch):
    ctx = _ctx(monkeypatch, tables={"crmbrain.ticker": [{"source": "heyreach"}]})
    assert resolve_lead_source(_ev(), ctx) == ("LinkedIn/HeyReach", "confirmed")


@pytest.mark.parametrize(
    "camp,expected",
    [
        ("LiveLeads | Staffing Owners | 2026-10", "Live leads"),
        ("SG Influencer Partnerships 1007", "Partner play"),
        ("SG Referral Hosts 1015 - PE", "Referral"),
        ("SalesGlider Staffing", "Cold email"),
    ],
)
def test_smartlead_lookup_reply_is_confirmed(monkeypatch, camp, expected):
    camps = [{"campaign_name": camp, "last_reply_at": "2026-10-09T17:36:51Z", "last_sent_at": "2026-10-09T14:00:00Z"}]
    assert resolve_lead_source(_ev(), _ctx(monkeypatch, campaigns=camps)) == (expected, "confirmed")


def test_smartlead_lookup_sent_only_is_inferred_unless_booking_has_send_id(monkeypatch):
    camps = [{"campaign_name": "LiveLeads | Contractor Owners | 2026-10", "last_sent_at": "2026-10-09T14:00:00Z"}]
    assert resolve_lead_source(_ev(), _ctx(monkeypatch, campaigns=camps)) == ("Live leads", "inferred")
    tables = {"public.booking_events": [{"send_id": "abc", "referrer": None}]}
    assert resolve_lead_source(_ev(), _ctx(monkeypatch, tables=tables, campaigns=camps)) == ("Live leads", "confirmed")


def test_most_recent_reply_wins_across_campaigns(monkeypatch):
    camps = [
        {"campaign_name": "SalesGlider Staffing", "last_reply_at": "2026-08-01T00:00:00Z"},
        {"campaign_name": "SG Influencer Partnerships 1007", "last_reply_at": "2026-10-09T00:00:00Z"},
    ]
    assert resolve_lead_source(_ev(), _ctx(monkeypatch, campaigns=camps)) == ("Partner play", "confirmed")


def test_send_id_without_campaign_data_is_cold_email_confirmed(monkeypatch):
    tables = {"public.booking_events": [{"send_id": "abc"}]}
    assert resolve_lead_source(_ev(), _ctx(monkeypatch, tables=tables)) == ("Cold email", "confirmed")


def test_weak_history_is_inferred(monkeypatch):
    ctx = _ctx(monkeypatch, tables={"crmbrain.ticker": [{"source": "hubspot"}, {"source": "smartlead"}]})
    assert resolve_lead_source(_ev(), ctx) == ("Cold email", "inferred")
    contact = {"properties": {"lead_source_platofrm": "HeyReach"}}
    assert resolve_lead_source(_ev(), _ctx(monkeypatch), contact) == ("LinkedIn/HeyReach", "inferred")


# ---- Inbound/other -------------------------------------------------------------------------------


def test_site_form_conversion_is_inbound_confirmed(monkeypatch):
    contact = {"properties": {"first_conversion_event_name": "Contact us form"}}
    assert resolve_lead_source(_ev(), _ctx(monkeypatch), contact) == ("Inbound/other", "confirmed")


def test_booking_from_our_site_is_inbound_confirmed(monkeypatch):
    tables = {"public.booking_events": [{"send_id": None, "referrer": "https://www.salesglidergrowth.com/book"}]}
    assert resolve_lead_source(_ev(), _ctx(monkeypatch, tables=tables)) == ("Inbound/other", "confirmed")


def test_direct_calendly_booking_with_no_outbound_touch_is_inbound_inferred(monkeypatch):
    assert resolve_lead_source(_ev(), _ctx(monkeypatch)) == ("Inbound/other", "inferred")
    contact = {"properties": {"hs_analytics_source": "ORGANIC_SEARCH"}}
    assert resolve_lead_source(_ev("gmail"), _ctx(monkeypatch), contact) == ("Inbound/other", "inferred")


def test_unreadable_allo_is_a_gap_not_an_error(monkeypatch):
    mem = _Mem()

    def sb(schema, method, table, params=None, **kw):
        if table in ("calls", "allo_calls_v"):
            raise RuntimeError("PGRST106 schema not exposed")
        return []

    mem._sb_named = sb
    ev = _ev()
    ev.phone = "9735550142"
    ctx = _ctx(monkeypatch, memory=mem)
    # Allo is blind, so a direct booking is not called inbound (it could be a cold-call booking) ...
    assert resolve_lead_source(ev, ctx) is None
    # ... but a site form is still confirmed inbound.
    contact = {"properties": {"first_conversion_event_name": "Contact us form"}}
    assert resolve_lead_source(ev, ctx, contact) == ("Inbound/other", "confirmed")


def test_allo_falls_back_to_public_view(monkeypatch):
    mem = _Mem()

    def sb(schema, method, table, params=None, **kw):
        if schema == "allo":
            raise RuntimeError("PGRST106")
        return [{"id": "c1"}] if table == "allo_calls_v" else []

    mem._sb_named = sb
    ev = _ev()
    ev.phone = "9735550142"
    assert resolve_lead_source(ev, _ctx(monkeypatch, memory=mem)) == ("Cold call", "confirmed")


def test_failed_lookup_never_guesses_inbound(monkeypatch):
    assert resolve_lead_source(_ev(), _ctx(monkeypatch, fail_sl=True)) is None
    assert resolve_lead_source(_ev(), _ctx(monkeypatch, memory=_Mem(fail=True))) is None


def test_truly_nothing_is_blank(monkeypatch):
    assert resolve_lead_source(_ev("gmail"), _ctx(monkeypatch)) is None
    assert resolve_lead_source(_ev("gmail", email=""), None) is None


# ---- HubSpot write path --------------------------------------------------------------------------


class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body or {}
        self.text = ""

    def json(self):
        return self._body


def _hs(existing=None, ctx=None):
    hs = HubSpot.__new__(HubSpot)
    hs.dry_run = False
    hs.lead_source_ctx = ctx
    calls = []

    def fake(method, path, **kw):
        calls.append((method, path, kw.get("json")))
        if method == "GET":
            return _Resp(200, {"properties": {"sg_lead_source": existing}})
        return _Resp(200)

    hs._request = fake
    return hs, calls


def test_stamp_sets_blank_deal_and_contact(monkeypatch):
    hs, calls = _hs(ctx=_ctx(monkeypatch, tables={"public.booking_events": [{"send_id": "x"}]}))
    out = hs.stamp_lead_source({"id": "1", "properties": {}}, {"id": "2", "properties": {}}, _ev())
    assert out == ("Cold email", "confirmed")
    patches = [c for c in calls if c[0] == "PATCH"]
    assert [p[1].split("/")[4] for p in patches] == ["deals", "contacts"]
    assert patches[0][2]["properties"] == {"sg_lead_source": "Cold email", "sg_lead_source_confidence": "confirmed"}


def test_stamp_never_overwrites_and_skips_dry_run(monkeypatch):
    hs, calls = _hs(existing="Referral", ctx=_ctx(monkeypatch))
    assert hs.stamp_lead_source({"id": "1"}, {"id": "2"}, _ev()) is None
    assert not [c for c in calls if c[0] == "PATCH"]
    hs.dry_run = True
    assert hs.stamp_lead_source({"id": "1"}, None, _ev()) is None


def test_stamp_leaves_blank_when_no_evidence(monkeypatch):
    hs, calls = _hs(ctx=_ctx(monkeypatch))
    assert hs.stamp_lead_source({"id": "1"}, {"id": "2"}, _ev("gmail")) is None
    assert not [c for c in calls if c[0] == "PATCH"]


def test_commit_deal_write_stamps_when_hubspot_supports_it():
    class Fake:
        stamped = None

        def upsert_deal(self, contact, ev, stage, amount=""):
            return {"id": "9"}

        def stamp_lead_source(self, deal, contact, ev):
            Fake.stamped = deal["id"]

    assert commit_deal_write(Fake(), {"id": "1"}, _ev(), "x") == {"id": "9"}
    assert Fake.stamped == "9"
