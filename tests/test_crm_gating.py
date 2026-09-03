from io import BytesIO
from pathlib import Path
import zipfile

from crmbrain.config import STAGE, Settings, is_personal
from crmbrain.cycle import _handle_engagement
from crmbrain.memory import Memory
from crmbrain.models import CycleReport, Engagement
from crmbrain.policy import (
    choose_deal_action,
    contact_has_meeting_evidence,
    is_blank_contact,
    is_cube_business_discovery,
    may_create_hubspot_contact,
    may_write_hubspot,
    personal_allowed_for_sales_intro,
    resolve_stage,
    should_move_stage,
)
from crmbrain.prune import prune_blank_contacts, prune_replied_deals
from crmbrain.sources.cube_acr import docx_text, file_kind
from crmbrain.sources.gmail_scan import _stage_from_mail


def make_settings(**kwargs) -> Settings:
    base = dict(
        hubspot_token="tok",
        gmail_client_id="",
        gmail_client_secret="",
        gmail_refresh_token="",
        josh_brief_email="joshua@salesglidergrowth.com",
        fireflies_key="",
        smartlead_key="",
        cube_folder="folder",
        heyreach_url="https://mcp.heyreach.io/mcp",
        heyreach_key="",
        heyreach_campaign_id=1,
        heyreach_linkedin_account_id=1,
        enrichment_url="",
        enrichment_client_tag="salesglider",
        leadmagic_key="",
        slack_token="",
        slack_channel="C0BHBDTMRFY",
        supabase_url="https://example.supabase.co",
        supabase_key="",
        gemini_key="",
        gemini_model="gemini-2.5-flash",
        allo_url="",
        allo_key="",
        lookback_hours=36,
    )
    base.update(kwargs)
    return Settings(**base)


class FakeHubSpot:
    def __init__(self, contacts=None):
        self.contacts = list(contacts or [])
        self.deals = []
        self.notes = []
        self.patches = []
        self.writes = []
        self._n = 10

    def find_contact(self, email="", phone="", name=""):
        email_l = (email or "").lower()
        digits = "".join(c for c in (phone or "") if c.isdigit())
        for row in self.contacts:
            props = row.get("properties") or {}
            if email_l and (props.get("email") or "").lower() == email_l:
                return row
            other = "".join(c for c in (props.get("phone") or "") if c.isdigit())
            if digits and len(digits) >= 10 and other[-10:] == digits[-10:]:
                return row
        return None

    def in_crm(self, email="", phone=""):
        return self.find_contact(email=email, phone=phone) is not None

    def upsert_contact(self, ev):
        existing = self.find_contact(email=ev.email, phone=ev.phone)
        self.writes.append(("upsert_contact", ev.source, ev.email or ev.phone))
        if existing:
            return existing
        self._n += 1
        row = {
            "id": str(self._n),
            "properties": {
                "email": ev.email,
                "firstname": ev.first_name,
                "lastname": ev.last_name,
                "phone": ev.phone,
                "company": ev.company,
                "crm_source": ev.source,
            },
        }
        self.contacts.append(row)
        return row

    def patch_contact(self, contact_id, properties):
        self.patches.append((contact_id, properties))

    def add_note(self, contact_id, body):
        self.notes.append((contact_id, body))

    def open_deals_for_contact(self, contact_id):
        return [d for d in self.deals if d.get("contact_id") == str(contact_id)]

    def iter_deals(self, properties, stage=""):
        for deal in self.deals:
            if stage and (deal.get("properties") or {}).get("dealstage") != stage:
                continue
            yield deal

    def contacts_for_deal(self, deal_id):
        deal = next((d for d in self.deals if d["id"] == deal_id), None)
        if not deal:
            return []
        return [c for c in self.contacts if c["id"] == deal.get("contact_id")]

    def contact_has_meetings(self, contact_id):
        return bool(getattr(self, "meetings", {}).get(str(contact_id)))

    def archive_deal(self, deal_id):
        self.deals = [d for d in self.deals if d["id"] != deal_id]
        self.writes.append(("archive_deal", deal_id))

    def archive_contact(self, contact_id):
        self.contacts = [c for c in self.contacts if c["id"] != str(contact_id)]
        self.writes.append(("archive_contact", contact_id))

    def move_deal(self, deal_id, stage, evidence=""):
        for deal in self.deals:
            if deal["id"] == deal_id:
                deal["properties"]["dealstage"] = stage
        self.writes.append(("move_deal", deal_id, stage))

    def iter_contacts(self, properties):
        yield from self.contacts

    def upsert_deal(self, contact, ev, stage):
        self.writes.append(("upsert_deal", ev.source, stage))
        live = self.open_deals_for_contact(contact["id"])
        if live:
            deal = live[0]
            current = (deal.get("properties") or {}).get("dealstage") or ""
            target = choose_deal_action(current, stage, ev)
            if target:
                deal["properties"]["dealstage"] = target
            return deal
        target = choose_deal_action(None, stage, ev)
        if not target:
            return {}
        deal = {
            "id": f"d{len(self.deals) + 1}",
            "contact_id": contact["id"],
            "properties": {"dealstage": target, "dealname": ev.display_name()},
        }
        self.deals.append(deal)
        return deal


def _handle(tmp_path: Path, ev: Engagement, hs: FakeHubSpot | None = None):
    settings = make_settings()
    memory = Memory(settings, data_dir=tmp_path)
    report = CycleReport()
    hs = hs or FakeHubSpot()
    _handle_engagement(ev, settings, hs, memory, None, report)
    return hs, memory, report


def test_smartlead_without_meeting_skips_hubspot(tmp_path):
    ev = Engagement(
        source="smartlead",
        external_id="sl-1",
        email="pat@acme.com",
        first_name="Pat",
        last_name="Lee",
        company="Acme",
        summary="Positive SmartLead reply (Interested) in SG HVAC",
    )
    assert not may_create_hubspot_contact(ev)
    assert not may_write_hubspot(ev, already_in_crm=False)
    assert resolve_stage(ev) == ""
    hs, memory, report = _handle(tmp_path, ev)
    assert hs.writes == []
    assert hs.contacts == []
    assert hs.deals == []
    assert any("no meeting, skip HubSpot" in s for s in report.skipped)
    assert any("never_booked" in t for t in report.ticker_enrolled)
    assert "smartlead:sl-1" in memory._local["processed"]


def test_heyreach_and_rvm_never_open_replied_deals():
    hey = Engagement(source="heyreach", external_id="hr-1", first_name="Sam", last_name="Reed", linkedin_url="https://www.linkedin.com/in/samreed")
    rvm = Engagement(source="rvm", external_id="rvm-1", phone="+15551234567", summary="RVM callback")
    assert resolve_stage(hey) == ""
    assert resolve_stage(rvm) == ""
    assert choose_deal_action(None, STAGE["replied"], hey) is None
    assert choose_deal_action(None, STAGE["replied"], rvm) is None
    assert choose_deal_action(STAGE["discovery_scheduled"], STAGE["replied"], hey) is None


def test_gmail_person_cold_skips_hubspot(tmp_path):
    ev = Engagement(
        source="gmail_person",
        external_id="gm-1",
        email="cold@example.com",
        first_name="Cold",
        last_name="Lead",
    )
    hs, _, report = _handle(tmp_path, ev)
    assert hs.writes == []
    assert report.ticker_enrolled == []
    assert any("no meeting, skip HubSpot" in s for s in report.skipped)


def test_calendly_creates_contact_and_discovery_scheduled(tmp_path):
    ev = Engagement(
        source="calendly",
        external_id="cal-1",
        email="lklein@grnplano.com",
        first_name="Laura",
        last_name="Klein",
        company="GRN Plano",
        raw_subject="New Event: Laura Klein - SalesGlider Intro",
        extra={"event_type": "SalesGlider Intro"},
    )
    assert may_create_hubspot_contact(ev)
    assert resolve_stage(ev) == STAGE["discovery_scheduled"]
    hs, _, report = _handle(tmp_path, ev)
    assert any(w[0] == "upsert_contact" for w in hs.writes)
    assert hs.deals[0]["properties"]["dealstage"] == STAGE["discovery_scheduled"]
    assert any("qualifiedtobuy" in d or "discovery" in d.lower() for d in report.deals_moved)


def test_fireflies_held_is_discovery_completed(tmp_path):
    ev = Engagement(
        source="fireflies",
        external_id="ff-1",
        email="rob@cyberguard360.com",
        name="Robert Lawson",
        transcript="We walked the discovery agenda and their MSP pipeline.",
        raw_subject="Robert Lawson and Joshua Osborn",
    )
    assert resolve_stage(ev) == STAGE["discovery_completed"]
    hs, _, report = _handle(tmp_path, ev)
    assert hs.contacts
    assert hs.deals[0]["properties"]["dealstage"] == STAGE["discovery_completed"]
    assert report.contacts_upserted


def test_cube_disco_transcript_creates_completed_deal():
    ev = Engagement(
        source="cube_acr",
        external_id="cu-1",
        phone="+15559876543",
        transcript="This is a SalesGlider discovery call about their roofing pipeline and campaign." * 2,
        raw_subject="2026-09-03 SalesGlider Intro",
        extra={"transcript_kind": "docx_transcript"},
    )
    assert is_cube_business_discovery(ev)
    assert may_create_hubspot_contact(ev)
    assert resolve_stage(ev) == STAGE["discovery_completed"]


def test_cube_html_or_family_is_not_hubspot():
    html = Engagement(
        source="cube_acr",
        external_id="cu-html",
        phone="+15559876543",
        transcript="<!DOCTYPE html><html><body>scraped drive page</body></html>" + (" x" * 40),
    )
    family = Engagement(
        source="cube_acr",
        external_id="cu-fam",
        phone="+15551112222",
        transcript="Hey love you, what's for dinner after soccer practice? I'll pick up the kids." + (" x" * 20),
    )
    assert not is_cube_business_discovery(html)
    assert not may_create_hubspot_contact(html)
    assert not is_cube_business_discovery(family)
    assert resolve_stage(html) == ""
    assert resolve_stage(family) == ""


def test_allo_non_disco_no_default_stage():
    ev = Engagement(source="allo", external_id="al-1", email="a@b.com", summary="Quick Allo ping", raw_subject="Allo call")
    assert not may_create_hubspot_contact(ev)
    assert resolve_stage(ev) == ""


def test_smartlead_never_defaults_nurture_deal():
    ev = Engagement(source="smartlead", external_id="sl-2", email="pat@acme.com", first_name="Pat")
    assert resolve_stage(ev) == ""
    assert resolve_stage(ev, {"stage_hint": "nurture"}) == ""


def test_no_stage_regression_to_replied_or_nurture():
    ev = Engagement(source="heyreach", external_id="x", email="a@b.com")
    assert not should_move_stage(STAGE["discovery_scheduled"], STAGE["replied"])
    assert not should_move_stage(STAGE["discovery_completed"], STAGE["nurture"])
    assert should_move_stage(STAGE["discovery_scheduled"], STAGE["nurture"], back_signal=True)
    assert should_move_stage(STAGE["replied"], STAGE["discovery_completed"])
    assert choose_deal_action(STAGE["discovery_scheduled"], STAGE["replied"], ev) is None
    assert choose_deal_action(STAGE["proposal_sent"], STAGE["discovery_completed"], ev) is None
    held = Engagement(
        source="fireflies",
        external_id="ff-2",
        email="a@b.com",
        transcript="Discovery completed.",
    )
    assert choose_deal_action(STAGE["replied"], STAGE["replied"], held) == STAGE["discovery_completed"]
    assert choose_deal_action(STAGE["discovery_scheduled"], STAGE["discovery_completed"], held) == STAGE["discovery_completed"]


def test_heyreach_on_existing_meeting_contact_does_not_open_replied(tmp_path):
    hs = FakeHubSpot(
        [
            {
                "id": "99",
                "properties": {
                    "email": "lklein@grnplano.com",
                    "firstname": "Laura",
                    "crm_source": "calendly",
                },
            }
        ]
    )
    hs.deals.append(
        {
            "id": "d-existing",
            "contact_id": "99",
            "properties": {"dealstage": STAGE["discovery_scheduled"], "dealname": "Laura"},
        }
    )
    ev = Engagement(
        source="heyreach",
        external_id="hr-existing",
        email="lklein@grnplano.com",
        first_name="Laura",
        last_name="Klein",
        summary="LinkedIn chat",
    )
    hs, _, report = _handle(tmp_path, ev, hs=hs)
    assert hs.deals[0]["properties"]["dealstage"] == STAGE["discovery_scheduled"]
    assert not any(w[0] == "upsert_deal" and w[2] == STAGE["replied"] for w in hs.writes)


def test_jeremy_personal_except_salesglider_intro():
    assert is_personal(name="Jeremy Ciotola")
    assert is_personal(phone="+19733030001")
    cold = Engagement(source="heyreach", external_id="j1", name="Jeremy Ciotola", phone="+19733030001")
    assert not personal_allowed_for_sales_intro(cold)
    intro = Engagement(
        source="calendly",
        external_id="j2",
        name="Jeremy Ciotola",
        email="jeremy@example.com",
        raw_subject="New Event: Jeremy Ciotola - SalesGlider Intro",
        extra={"event_type": "SalesGlider Intro"},
    )
    assert personal_allowed_for_sales_intro(intro)
    assert may_create_hubspot_contact(intro)


def test_jeremy_intro_writes_hubspot(tmp_path):
    ev = Engagement(
        source="calendly",
        external_id="j-cal",
        name="Jeremy Ciotola",
        first_name="Jeremy",
        last_name="Ciotola",
        email="jeremy@example.com",
        phone="+19733030001",
        raw_subject="New Event: Jeremy Ciotola - SalesGlider Intro",
        extra={"event_type": "SalesGlider Intro"},
    )
    hs, _, report = _handle(tmp_path, ev)
    assert hs.contacts
    assert hs.deals[0]["properties"]["dealstage"] == STAGE["discovery_scheduled"]
    assert not any("personal" == s.split()[-1] and "skip HubSpot" not in s for s in report.skipped if "personal" in s)


def test_gcal_and_calendly_stage_from_mail():
    assert _stage_from_mail("New Event", "Calendly", "accepted") == STAGE["discovery_scheduled"]
    assert _stage_from_mail("Invitation: SalesGlider Intro", "calendar-notification@google.com", "scheduled") == (
        STAGE["discovery_scheduled"]
    )
    assert _stage_from_mail("Invitee no-show", "Calendly", "no-show") == STAGE["no_show"]


def test_meeting_evidence_and_blank_contact():
    meeting = {"id": "1", "properties": {"crm_source": "calendly", "email": "a@b.com"}}
    blank = {"id": "2", "properties": {"email": "", "firstname": "", "lastname": "", "phone": "", "company": ""}}
    replied = [{"properties": {"dealstage": STAGE["replied"]}}]
    live = [{"properties": {"dealstage": STAGE["discovery_scheduled"]}}]
    assert contact_has_meeting_evidence(meeting, replied)
    assert contact_has_meeting_evidence({"id": "3", "properties": {"crm_source": "heyreach"}}, live)
    assert not contact_has_meeting_evidence({"id": "4", "properties": {"crm_source": "heyreach"}}, replied)
    assert is_blank_contact(blank)
    assert not is_blank_contact(meeting)


def test_docx_text_and_file_kind():
    body = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>SalesGlider discovery transcript</w:t></w:r></w:p></w:body>"
        "</w:document>"
    )
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", body)
    assert "discovery transcript" in docx_text(buf.getvalue())
    assert file_kind("2026-09-03-rob-transcript.docx") == "docx_transcript"
    assert file_kind("notes.txt") == "text"
    assert file_kind("meta.json") == "json"


def test_prune_archives_replied_without_meeting_and_promotes_held():
    hs = FakeHubSpot(
        [
            {"id": "1", "properties": {"email": "junk@x.com", "crm_source": "heyreach", "firstname": "Junk"}},
            {"id": "2", "properties": {"email": "held@x.com", "crm_source": "fireflies", "firstname": "Held"}},
        ]
    )
    hs.deals = [
        {"id": "junk-deal", "contact_id": "1", "properties": {"dealstage": STAGE["replied"], "dealname": "Junk"}},
        {"id": "held-deal", "contact_id": "2", "properties": {"dealstage": STAGE["replied"], "dealname": "Held"}},
    ]
    report = CycleReport()
    prune_replied_deals(hs, report)
    ids = {d["id"] for d in hs.deals}
    assert "junk-deal" not in ids
    assert "held-deal" in ids
    assert hs.deals[0]["properties"]["dealstage"] == STAGE["discovery_completed"]
    assert any("Junk" in x for x in report.deals_pruned)


def test_prune_skips_contacts_with_meeting_evidence():
    hs = FakeHubSpot(
        [
            {"id": "blank", "properties": {"email": "", "firstname": "", "lastname": "", "phone": "", "company": ""}},
            {"id": "meet", "properties": {"email": "", "firstname": "", "lastname": "", "phone": "", "company": "", "crm_source": "calendly"}},
        ]
    )
    report = CycleReport()
    prune_blank_contacts(hs, report)
    ids = {c["id"] for c in hs.contacts}
    assert "blank" not in ids
    assert "meet" in ids
    assert "blank" in report.contacts_pruned
