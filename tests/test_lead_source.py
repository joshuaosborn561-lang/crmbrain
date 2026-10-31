from crmbrain.deal_write import commit_deal_write
from crmbrain.hubspot import CONTACT_PROPS, DEAL_PROPS, HubSpot
from crmbrain.lead_source import LEAD_SOURCES, resolve_lead_source
from crmbrain.models import Engagement


class _Mem:
    use_supabase = True

    def __init__(self, tables):
        self.tables = tables

    def _sb_named(self, schema, method, table, params=None, **kw):
        return self.tables.get(table, [])


def _ev(source="calendly", email="a@b.com"):
    return Engagement(source=source, external_id="1", email=email)


def test_property_definitions_cover_all_sources():
    for props in (CONTACT_PROPS, DEAL_PROPS):
        by = {p["name"]: p for p in props}
        assert [o["value"] for o in by["sg_lead_source"]["options"]] == list(LEAD_SOURCES)
        assert [o["value"] for o in by["sg_lead_source_confidence"]["options"]] == ["confirmed", "inferred"]


def test_direct_channels_are_confirmed():
    assert resolve_lead_source(_ev("smartlead")) == ("Cold email", "confirmed")
    assert resolve_lead_source(_ev("heyreach")) == ("LinkedIn/HeyReach", "confirmed")
    assert resolve_lead_source(_ev("allo")) == ("Cold call", "confirmed")


def test_booking_with_smartlead_send_id_is_cold_email_confirmed():
    mem = _Mem({"booking_events": [{"send_id": "abc"}]})
    assert resolve_lead_source(_ev(), mem) == ("Cold email", "confirmed")


def test_ticker_history_is_inferred():
    mem = _Mem({"booking_events": [], "ticker": [{"source": "hubspot"}, {"source": "heyreach"}]})
    assert resolve_lead_source(_ev(), mem) == ("LinkedIn/HeyReach", "inferred")


def test_contact_platform_is_inferred_and_no_evidence_is_none():
    contact = {"properties": {"lead_source_platofrm": "Smartlead"}}
    assert resolve_lead_source(_ev(), _Mem({}), contact) == ("Cold email", "inferred")
    assert resolve_lead_source(_ev(), _Mem({}), {"properties": {}}) is None


class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body or {}
        self.text = ""

    def json(self):
        return self._body


def _hs(existing=None):
    hs = HubSpot.__new__(HubSpot)
    hs.dry_run = False
    hs.lead_source_memory = _Mem({"booking_events": [{"send_id": "x"}]})
    calls = []

    def fake(method, path, **kw):
        calls.append((method, path, kw.get("json")))
        if method == "GET":
            return _Resp(200, {"properties": {"sg_lead_source": existing}})
        return _Resp(200)

    hs._request = fake
    return hs, calls


def test_stamp_sets_blank_deal_and_contact():
    hs, calls = _hs()
    out = hs.stamp_lead_source({"id": "1", "properties": {}}, {"id": "2", "properties": {}}, _ev())
    assert out == ("Cold email", "confirmed")
    patches = [c for c in calls if c[0] == "PATCH"]
    assert len(patches) == 2
    assert patches[0][2]["properties"] == {"sg_lead_source": "Cold email", "sg_lead_source_confidence": "confirmed"}


def test_stamp_never_overwrites_and_skips_dry_run():
    hs, calls = _hs(existing="Referral")
    assert hs.stamp_lead_source({"id": "1", "properties": {}}, {"id": "2", "properties": {}}, _ev()) is None
    assert not [c for c in calls if c[0] == "PATCH"]
    hs.dry_run = True
    assert hs.stamp_lead_source({"id": "1"}, None, _ev()) is None


def test_commit_deal_write_stamps_when_hubspot_supports_it():
    class Fake:
        stamped = None

        def upsert_deal(self, contact, ev, stage, amount=""):
            return {"id": "9"}

        def stamp_lead_source(self, deal, contact, ev):
            Fake.stamped = deal["id"]

    assert commit_deal_write(Fake(), {"id": "1"}, _ev(), "x") == {"id": "9"}
    assert Fake.stamped == "9"
