"""Cube Drive API v3 listing, personal-number skip, non-deal exclusion, dry-run."""

from datetime import date, datetime, timedelta, timezone
from io import BytesIO

from crmbrain.config import CDT, is_non_deal_person, is_personal, now_utc, personal_numbers
from crmbrain.cycle import (
    _backfill_hubspot_invites,
    _handle_engagement,
    apply_gmail_stage_update,
    integration_status,
    run as cycle_run,
)
from crmbrain.gmail_client import CALENDAR_READONLY_SCOPE
from crmbrain.google_auth import CALENDAR_READONLY, DRIVE_READONLY, report_scopes
from crmbrain.hubspot import HubSpot
from crmbrain.memory import Memory
from crmbrain.models import CycleReport, Engagement
from crmbrain.budget import DEFAULT_MAX_CREATES
from crmbrain.policy import (
    contact_is_prospect,
    cube_has_sales_intent,
    is_cube_business_discovery,
    resolve_stage,
)
from crmbrain.config import STAGE
from crmbrain.sources import cube_acr, gmail_scan
from crmbrain.sources.cube_acr import (
    CubeAuthError,
    DriveAuth,
    cube_call_key,
    docx_text,
    list_transcript_candidates,
    parse_cube_title,
    resolve_drive_auth,
    should_skip_cube_call,
    to_e164,
)
from crmbrain.staleness import WATCHED_SOURCES
from tests.test_crm_gating import FakeHubSpot, make_settings

TYLER_TITLE = "Tyler Cook (+1 419-705-5122) ↗ (phone) 2026-09-30 13-17-54 - transcript.docx"
PHONE_ONLY_TITLE = "(310) 991-2017 ↗ (phone) 2026-09-30 14-33-47 - transcript"
NONNA_TITLE = "Nonna Iphone (+1 973-461-3447) ↗ (phone) 2026-09-30 13-39-48 - transcript"
CAYDEN_TITLE = "Cayden (+1 561-225-5142) ↘ (phone) 2026-08-12 11-37-54 - transcript.docx"


ROOT = "cube-root-folder"
LATE_FOLDER = "folder-2026-09-29"
LATE_FILE = "file-late-docx"
DUP_FILE = "file-dup-docx"


def _folder(date: str, fid: str | None = None) -> dict:
    return {
        "id": fid or f"folder-{date}",
        "name": date,
        "mimeType": "application/vnd.google-apps.folder",
        "modifiedTime": f"{date}T18:00:00.000Z",
    }


def _docx_row(fid: str, name: str, folder_mtime: str = "2026-09-29T19:00:00.000Z", md5: str = "abc123") -> dict:
    return {
        "id": fid,
        "name": name,
        "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "modifiedTime": folder_mtime,
        "md5Checksum": md5,
    }


def test_cube_lists_past_html_50_folder_cap(monkeypatch):
    """HTML stopped at 2026-09-10 / 50 folders. API pages must see 2026-09-29."""
    start = date(2026, 8, 1)
    folders = [_folder((start + timedelta(days=i)).isoformat()) for i in range(66)]
    assert len(folders) == 66
    assert folders[49]["name"] == "2026-09-19"
    late = next(row for row in folders if row["name"] == "2026-09-29")
    late_id = late["id"]
    assert late_id == "folder-2026-09-29"

    calls = {"pages": 0, "children": []}

    def fake_get(auth, url, params=None):
        params = params or {}
        q = params.get("q") or ""
        token = params.get("pageToken") or ""
        if f"'{ROOT}' in parents" in q:
            calls["pages"] += 1
            if not token:
                return {"files": folders[:50], "nextPageToken": "page-2"}
            assert token == "page-2"
            return {"files": folders[50:], "nextPageToken": ""}
        if f"'{late_id}' in parents" in q:
            calls["children"].append(late_id)
            return {
                "files": [
                    _docx_row(
                        LATE_FILE,
                        "Tyler Cook (+1 419-705-5122) ↗ (phone) 2026-09-29 13-17-54 - transcript.docx",
                    )
                ]
            }
        return {"files": []}

    monkeypatch.setattr(cube_acr, "drive_get", fake_get)
    settings = make_settings(
        cube_folder=ROOT,
        google_api_key="public-folder-key",
        cube_lookback_days=14,
        lookback_start_at=datetime(2026, 9, 16, tzinfo=timezone.utc),
    )
    auth = DriveAuth(mode="api_key", headers={}, key="public-folder-key")
    found = list_transcript_candidates(settings, auth, page_size=50)
    assert calls["pages"] == 2
    assert late_id in calls["children"]
    assert any(f.file_id == LATE_FILE for f in found)
    assert any("2026-09-29" in f.name for f in found)
    assert not any(f.folder_date == "2026-09-10" and f.file_id for f in found if f.file_id == LATE_FILE)


def test_parse_cube_title_real_filenames():
    from docx import Document

    tyler = parse_cube_title(TYLER_TITLE)
    assert tyler == {
        "name": "Tyler Cook",
        "phone": "+14197055122",
        "date": "2026-09-30",
        "time": "13:17:54",
    }
    phone_only = parse_cube_title(PHONE_ONLY_TITLE)
    assert phone_only == {
        "name": "",
        "phone": "+13109912017",
        "date": "2026-09-30",
        "time": "14:33:47",
    }
    nonna = parse_cube_title(NONNA_TITLE)
    assert nonna["name"] == "Nonna Iphone"
    assert nonna["phone"] == "+19734613447"
    assert nonna["date"] == "2026-09-30"
    assert nonna["time"] == "13:39:48"
    cayden = parse_cube_title(CAYDEN_TITLE)
    assert cayden["name"] == "Cayden"
    assert cayden["phone"] == "+15612255142"
    assert cayden["date"] == "2026-08-12"
    assert cayden["time"] == "11:37:54"
    assert to_e164("(310) 991-2017") == "+13109912017"

    local = datetime(2026, 9, 30, 13, 17, 54, tzinfo=CDT)
    item = cube_acr.DriveFile(file_id="x", name=TYLER_TITLE, mime_type="docx")
    assert cube_acr._occurred_at(tyler, item) == local.astimezone(timezone.utc)

    doc = Document()
    doc.add_paragraph("SalesGlider discovery call about their roofing pipeline.")
    buf = BytesIO()
    doc.save(buf)
    text = docx_text(buf.getvalue())
    assert "SalesGlider discovery" in text
    assert "roofing pipeline" in text


def test_personal_number_nonna_is_skipped():
    assert "+19734613447" in personal_numbers()
    assert is_personal(phone="+1 973-461-3447")
    assert is_personal(phone="19734613447")
    assert is_personal(name="Nonna")
    nonna = parse_cube_title(NONNA_TITLE)
    cayden = parse_cube_title(CAYDEN_TITLE)
    assert should_skip_cube_call(name=nonna["name"], phone=nonna["phone"], title=NONNA_TITLE)
    assert should_skip_cube_call(name=cayden["name"], phone=cayden["phone"], title=CAYDEN_TITLE)
    tyler = parse_cube_title(TYLER_TITLE)
    assert not should_skip_cube_call(name=tyler["name"], phone=tyler["phone"], title=TYLER_TITLE)


def test_scan_skips_personal_and_dedupes_same_call_two_accounts(monkeypatch):
    items = [
        cube_acr.DriveFile(
            file_id="gdoc-copy",
            name=TYLER_TITLE.replace(" - transcript.docx", ""),
            mime_type="application/vnd.google-apps.document",
            modified_time=datetime(2026, 9, 30, 19, tzinfo=timezone.utc),
            md5="md5-gdoc",
            folder_date="2026-09-30",
        ),
        cube_acr.DriveFile(
            file_id=LATE_FILE,
            name=TYLER_TITLE,
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            modified_time=datetime(2026, 9, 30, 19, 5, tzinfo=timezone.utc),
            md5="md5-docx",
            folder_date="2026-09-30",
        ),
        cube_acr.DriveFile(
            file_id="nonna-file",
            name=NONNA_TITLE,
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            modified_time=datetime(2026, 9, 30, 20, tzinfo=timezone.utc),
            md5="nonna-md5",
            folder_date="2026-09-30",
        ),
    ]
    assert cube_call_key(parse_cube_title(items[0].name)) == cube_call_key(parse_cube_title(TYLER_TITLE))
    assert items[0].md5 != items[1].md5

    monkeypatch.setattr(cube_acr, "resolve_drive_auth", lambda settings: DriveAuth("api_key", {}, "k"))
    monkeypatch.setattr(cube_acr, "list_transcript_candidates", lambda *a, **k: items)
    monkeypatch.setattr(
        cube_acr,
        "_load_text",
        lambda auth, item, kind: "SalesGlider discovery call about their roofing pipeline and owners.",
    )
    evs = cube_acr.scan(make_settings(cube_folder=ROOT, google_api_key="k"))
    assert [e.external_id for e in evs] == [LATE_FILE]
    assert evs[0].phone == "+14197055122"
    assert evs[0].display_name() == "Tyler Cook"
    assert evs[0].extra.get("transcript_kind") in {"docx", "docx_transcript"}


def test_exclusion_list_blocks_cynthia_and_alex(tmp_path):
    assert is_non_deal_person(name="Cynthia Hernandez")
    assert is_non_deal_person(email="cynthia@chorbie.com")
    assert is_non_deal_person(name="Alex Branning")
    assert not is_non_deal_person(name="Robert Lawson", email="rob@cyberguard360.com")

    settings = make_settings()
    memory = Memory(settings, data_dir=tmp_path)
    hs = FakeHubSpot()
    report = CycleReport()
    ev = Engagement(
        source="gmail_person",
        external_id="cynthia-mail",
        email="cynthia@chorbie.com",
        first_name="Cynthia",
        last_name="Hernandez",
        name="Cynthia Hernandez",
        company="Chorbie",
        raw_subject="Marketing Masterclass follow up",
        summary="Loved the masterclass",
    )
    _handle_engagement(ev, settings, hs, memory, None, report)
    assert hs.writes == []
    assert hs.contacts == []
    assert hs.deals == []
    assert any("excluded" in s for s in report.skipped)

    alex = Engagement(
        source="gmail",
        external_id="alex-cal",
        email="alex@example.com",
        name="Alex Branning",
        first_name="Alex",
        last_name="Branning",
        stage_hint="qualifiedtobuy",
        extra={"create_new": True},
    )
    apply_gmail_stage_update(alex, settings, hs, memory, None, CycleReport())
    assert hs.writes == []
    assert not any(c.get("properties", {}).get("email") == "cynthia@chorbie.com" for c in hs.contacts)


def test_gmail_scan_people_drops_excluded(monkeypatch):
    class FakeGmail:
        def search(self, query, max_results=40):
            return [{"id": "m1"}]

        def get(self, mid):
            return {
                "id": mid,
                "internalDate": "1759200000000",
                "snippet": "hey",
                "payload": {
                    "headers": [
                        {"name": "From", "value": "Cynthia Hernandez <cynthia@chorbie.com>"},
                        {"name": "To", "value": "joshua@salesglidergrowth.com"},
                        {"name": "Subject", "value": "Masterclass"},
                    ]
                },
            }

        def headers_map(self, message):
            return {h["name"].lower(): h.get("value", "") for h in message["payload"]["headers"]}

    people = gmail_scan.scan_people(make_settings(), FakeGmail())
    assert people == []


def test_hubspot_upsert_skips_excluded_person():
    settings = make_settings(dry_run=False, hubspot_token="tok")
    hs = HubSpot(settings)
    ev = Engagement(
        source="gmail_person",
        external_id="c1",
        email="cynthia@chorbie.com",
        first_name="Cynthia",
        last_name="Hernandez",
        name="Cynthia Hernandez",
    )
    out = hs.upsert_contact(ev)
    assert out.get("skipped") == "non_deal"
    assert hs.proposed == []


def test_cube_missing_auth_is_warning_not_present():
    settings = make_settings(cube_folder="1buFUvvaRUhfnu995tfI0s7FDBWsRFAnp")
    rows = integration_status(settings)
    assert "Cube ACR: missing" in rows
    assert "Cube ACR: present" not in rows
    assert "Allo key" not in " ".join(rows)
    try:
        resolve_drive_auth(settings)
        raise AssertionError("expected CubeAuthError")
    except CubeAuthError as exc:
        assert "GOOGLE_API_KEY" in str(exc)
    assert "cube_acr" in WATCHED_SOURCES
    assert "allo" not in WATCHED_SOURCES


def test_google_scopes_command_hides_secrets(monkeypatch, capsys):
    settings = make_settings(
        gmail_client_id="client-secret-looking",
        gmail_client_secret="super-secret-client",
        gmail_refresh_token="refresh-token-secret",
    )
    text = report_scopes(settings).as_text()
    assert "super-secret-client" not in text
    assert "refresh-token-secret" not in text
    assert "drive.readonly" in text
    assert "calendar.readonly" in text
    assert CALENDAR_READONLY_SCOPE == CALENDAR_READONLY
    assert DRIVE_READONLY.endswith("drive.readonly")

    monkeypatch.setattr("sys.argv", ["crmbrain", "google-scopes"])
    monkeypatch.setattr("crmbrain.__main__.Settings.from_env", lambda: settings)
    from crmbrain.__main__ import main

    assert main() == 0
    printed = capsys.readouterr().out
    assert "refresh-token-secret" not in printed
    assert "super-secret-client" not in printed


def test_dry_run_holds_with_cube_and_excluded_gmail(tmp_path, monkeypatch):
    settings = make_settings(
        dry_run=True, hubspot_token="tok", google_api_key="k", gmail_refresh_token="r"
    )
    hs = FakeHubSpot()
    memory = Memory(settings, data_dir=tmp_path)
    cube_ev = Engagement(
        source="cube_acr",
        external_id="cu-dry",
        phone="+15559876543",
        first_name="Rob",
        last_name="Lawson",
        transcript="SalesGlider discovery call about their roofing pipeline.",
        raw_subject="2026-09-29 Rob Lawson",
        extra={"transcript_kind": "docx_transcript", "skip_lookback": True},
        occurred_at=now_utc(),
    )
    cynthia = Engagement(
        source="gmail_person",
        external_id="cynthia-dry",
        email="cynthia@chorbie.com",
        first_name="Cynthia",
        last_name="Hernandez",
        name="Cynthia Hernandez",
        extra={"skip_lookback": True},
        occurred_at=now_utc(),
    )

    class Snap:
        upcoming = set()
        recent = set()
        create_engagements = []
        events = []
        calendar_api_ok = True
        calendar_api_error = ""

        def protect_emails(self):
            return set()

    monkeypatch.setattr("crmbrain.cycle.HubSpot", lambda settings: hs)
    monkeypatch.setattr("crmbrain.cycle.Memory", lambda settings: memory)
    monkeypatch.setattr("crmbrain.cycle.Gmail", lambda settings: object())
    monkeypatch.setattr("crmbrain.cycle.calendar_events.load_calendar", lambda *a, **k: Snap())
    monkeypatch.setattr("crmbrain.cycle.has_drive_access", lambda *a, **k: True)
    monkeypatch.setattr("crmbrain.cycle.cube_acr.scan", lambda *a, **k: [cube_ev])
    monkeypatch.setattr("crmbrain.cycle.fireflies.scan", lambda *a, **k: [])
    monkeypatch.setattr("crmbrain.cycle.smartlead.scan", lambda *a, **k: [])
    monkeypatch.setattr("crmbrain.cycle.rvm.scan", lambda *a, **k: [])
    monkeypatch.setattr("crmbrain.cycle.gmail_scan.scan_people", lambda *a, **k: [cynthia])
    monkeypatch.setattr("crmbrain.cycle.gmail_scan.scan", lambda *a, **k: [])
    monkeypatch.setattr(
        "crmbrain.cycle.enrichment.enrich",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("enrichment called")),
    )
    monkeypatch.setattr(
        "crmbrain.cycle.briefing.send_due",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("briefing called")),
    )

    report = cycle_run(settings)
    assert report.dry_run
    assert hs.writes == []
    assert hs.contacts == []
    assert hs.deals == []
    assert memory._local.get("cube_acr_calls") in (None, [])
    assert all(w == "cycle_runs" for w in memory.writes)
    assert memory._local["runs"][-1]["status"] == "dry_run"
    assert any("excluded" in s for s in report.skipped)
    assert not any("cynthia@chorbie.com" in (c.get("properties") or {}).get("email", "") for c in hs.contacts)


def test_family_intent_cube_call_is_skipped(tmp_path):
    ev = Engagement(
        source="cube_acr",
        external_id="fam-1",
        phone="+15551112222",
        first_name="Aunt",
        last_name="May",
        transcript="Hey love you, what's for dinner after soccer practice? I'll pick up the kids." + (" x" * 20),
        raw_subject="family catch up",
    )
    settings = make_settings()
    hs = FakeHubSpot()
    report = CycleReport()
    _handle_engagement(ev, settings, hs, Memory(settings, data_dir=tmp_path), None, report)
    assert hs.writes == []
    assert hs.contacts == []
    assert any("personal" in s or "family" in s for s in report.skipped)


def test_backfill_skips_cynthia_heyreach_and_patch():
    settings = make_settings(heyreach_key="k")
    hs = FakeHubSpot(
        [
            {
                "id": "cyn-1",
                "properties": {
                    "email": "cynthia@chorbie.com",
                    "firstname": "Cynthia",
                    "lastname": "Hernandez",
                    "company": "Chorbie",
                    "hs_linkedin_url": "https://www.linkedin.com/in/cynthiahernandez",
                },
            }
        ]
    )

    class FakeHey:
        def __init__(self):
            self.added = []

        def add_lead(self, ev):
            self.added.append(ev.email)
            return "queued"

    hey = FakeHey()
    report = CycleReport()
    from pathlib import Path
    import tempfile

    memory = Memory(settings, data_dir=Path(tempfile.mkdtemp()))
    _backfill_hubspot_invites(settings, hs, hey, memory, report)
    assert hey.added == []
    assert hs.patches == []
    assert any("excluded" in s for s in report.skipped)


def test_hubspot_patch_note_amount_skip_excluded():
    settings = make_settings(dry_run=True, hubspot_token="tok")
    hs = HubSpot(settings)
    ev = Engagement(
        source="gmail_person",
        external_id="c1",
        email="cynthia@chorbie.com",
        first_name="Cynthia",
        last_name="Hernandez",
        name="Cynthia Hernandez",
    )
    contact = {"id": "cyn-1", "properties": {"email": "cynthia@chorbie.com", "firstname": "Cynthia"}}
    hs.patch_contact("cyn-1", {"hs_linkedin_url": "https://linkedin.com/in/x"}, ev=ev, contact=contact)
    hs.add_note("cyn-1", "should not write", ev=ev, contact=contact)
    assert hs.fill_deal_amount({"id": "d1", "properties": {}}, "3000", ev=ev, contact=contact) is False
    assert hs.proposed == []


def test_upsert_cube_call_skips_existing_id(tmp_path):
    settings = make_settings()
    memory = Memory(settings, data_dir=tmp_path)
    ev = Engagement(source="cube_acr", external_id="same-id", phone="+14197055122", name="Tyler Cook")
    memory.upsert_cube_call(ev)
    memory.upsert_cube_call(ev)
    assert [row["id"] for row in memory._local["cube_acr_calls"]] == ["same-id"]


def test_cube_backfill_uses_14d_until_freshness(tmp_path, monkeypatch):
    settings = make_settings(dry_run=True, hubspot_token="tok", google_api_key="k", gmail_refresh_token="r")

    def old_call():
        return Engagement(
            source="cube_acr",
            external_id="old-tyler",
            phone="+14197055122",
            first_name="Tyler",
            last_name="Cook",
            name="Tyler Cook",
            transcript="SalesGlider discovery call about their roofing pipeline and the monthly retainer.",
            raw_subject=TYLER_TITLE,
            extra={"transcript_kind": "docx_transcript"},
            occurred_at=now_utc() - timedelta(days=10),
        )

    class Snap:
        upcoming = set()
        recent = set()
        create_engagements = []
        events = []
        calendar_api_ok = True
        calendar_api_error = ""

        def protect_emails(self):
            return set()

    def run_once(memory, hs):
        monkeypatch.setattr("crmbrain.cycle.HubSpot", lambda settings: hs)
        monkeypatch.setattr("crmbrain.cycle.Memory", lambda settings: memory)
        monkeypatch.setattr("crmbrain.cycle.Gmail", lambda settings: object())
        monkeypatch.setattr("crmbrain.cycle.calendar_events.load_calendar", lambda *a, **k: Snap())
        monkeypatch.setattr("crmbrain.cycle.has_drive_access", lambda *a, **k: True)
        monkeypatch.setattr("crmbrain.cycle.cube_acr.scan", lambda *a, **k: [old_call()])
        monkeypatch.setattr("crmbrain.cycle.fireflies.scan", lambda *a, **k: [])
        monkeypatch.setattr("crmbrain.cycle.smartlead.scan", lambda *a, **k: [])
        monkeypatch.setattr("crmbrain.cycle.rvm.scan", lambda *a, **k: [])
        monkeypatch.setattr("crmbrain.cycle.gmail_scan.scan_people", lambda *a, **k: [])
        monkeypatch.setattr("crmbrain.cycle.gmail_scan.scan", lambda *a, **k: [])
        return cycle_run(settings)

    first = Memory(settings, data_dir=tmp_path / "first")
    report = run_once(first, FakeHubSpot())
    assert any("backfill" in w for w in report.warnings)
    assert any(p.get("label") == "Tyler Cook" for p in report.proposed_writes)
    assert not any("outside window" in s for s in report.skipped if "old-tyler" in s)

    later = Memory(settings, data_dir=tmp_path / "later")
    later.dry_run = False
    later.record_freshness("cube_acr", last_item_at=now_utc())
    later.dry_run = True
    later_hs = FakeHubSpot()
    later_report = run_once(later, later_hs)
    assert later_hs.writes == []
    assert any("outside window" in s for s in later_report.skipped)


def test_cube_one_on_one_held_discovery_only_for_prospect_or_sales():
    routine = Engagement(
        source="cube_acr",
        external_id="ops-1",
        phone="+15551230000",
        first_name="Kyle",
        last_name="Peterson",
        name="Kyle Peterson",
        company="Roofs by Peterson",
        transcript=("Quick catch-up on the job site and next week's schedule. " * 8),
        raw_subject="Kyle Peterson 1:1",
    )
    assert not is_cube_business_discovery(routine)
    assert resolve_stage(routine) == ""

    one_on_one = Engagement(
        source="cube_acr",
        external_id="11-1",
        phone="+14197055122",
        first_name="Tyler",
        last_name="Cook",
        name="Tyler Cook",
        transcript=("Hey, just checking in on how things are going this week. " * 8),
        raw_subject=TYLER_TITLE,
    )
    assert not is_cube_business_discovery(one_on_one)
    one_on_one.extra["already_prospect"] = True
    assert is_cube_business_discovery(one_on_one)
    assert resolve_stage(one_on_one) == STAGE["discovery_completed"]

    sales = Engagement(
        source="cube_acr",
        external_id="sales-1",
        phone="+14197055122",
        first_name="Tyler",
        last_name="Cook",
        transcript=(
            "This is a SalesGlider discovery call about their roofing pipeline and campaign. "
            "They asked about the monthly retainer after we walk the owners."
        ),
        raw_subject=TYLER_TITLE,
    )
    assert is_cube_business_discovery(sales)
    assert resolve_stage(sales) == STAGE["discovery_completed"]


def _cycle_with_cube(tmp_path, monkeypatch, settings, hs, events, memory=None):
    class Snap:
        upcoming = set()
        recent = set()
        create_engagements = []
        events = []
        calendar_api_ok = True
        calendar_api_error = ""

        def protect_emails(self):
            return set()

    memory = memory or Memory(settings, data_dir=tmp_path)
    monkeypatch.setattr("crmbrain.cycle.HubSpot", lambda settings: hs)
    monkeypatch.setattr("crmbrain.cycle.Memory", lambda settings: memory)
    monkeypatch.setattr("crmbrain.cycle.Gmail", lambda settings: object())
    monkeypatch.setattr("crmbrain.cycle.calendar_events.load_calendar", lambda *a, **k: Snap())
    monkeypatch.setattr("crmbrain.cycle.has_drive_access", lambda *a, **k: True)
    monkeypatch.setattr("crmbrain.cycle.cube_acr.scan", lambda *a, **k: list(events))
    monkeypatch.setattr("crmbrain.cycle.fireflies.scan", lambda *a, **k: [])
    monkeypatch.setattr("crmbrain.cycle.smartlead.scan", lambda *a, **k: [])
    monkeypatch.setattr("crmbrain.cycle.rvm.scan", lambda *a, **k: [])
    monkeypatch.setattr("crmbrain.cycle.gmail_scan.scan_people", lambda *a, **k: [])
    monkeypatch.setattr("crmbrain.cycle.gmail_scan.scan", lambda *a, **k: [])
    return cycle_run(settings), memory, hs


def _sales_cube(i: int) -> Engagement:
    return Engagement(
        source="cube_acr",
        external_id=f"sale-{i}",
        email=f"lead{i}@prospect.com",
        phone=f"+1555101{i:04d}",
        first_name="Lead",
        last_name=str(i),
        name=f"Lead {i}",
        transcript=("SalesGlider discovery call about pricing and the monthly retainer. " * 6),
        raw_subject=f"Lead {i} discovery",
        extra={"transcript_kind": "docx_transcript", "skip_lookback": True},
        occurred_at=now_utc() - timedelta(hours=2),
    )


def test_backfill_over_max_creates_caps_and_reviews(tmp_path, monkeypatch):
    settings = make_settings(
        dry_run=True,
        hubspot_token="tok",
        google_api_key="k",
        gmail_refresh_token="r",
        max_creates=DEFAULT_MAX_CREATES,
    )
    events = [_sales_cube(i) for i in range(DEFAULT_MAX_CREATES + 5)]
    report, _, _ = _cycle_with_cube(tmp_path, monkeypatch, settings, FakeHubSpot(), events)
    creates = [p for p in report.proposed_writes if p.get("action") == "create"]
    assert len(events) > DEFAULT_MAX_CREATES
    assert len(creates) <= DEFAULT_MAX_CREATES
    assert sum(1 for x in report.review_queue if "cap" in x) >= 5


def test_paid_client_cube_campaign_leads_creates_no_deal(tmp_path):
    contact = {
        "id": "paid-1",
        "properties": {
            "email": "kyle@petersonroofs.com",
            "firstname": "Kyle",
            "lastname": "Peterson",
            "phone": "+15551230000",
            "company": "Roofs by Peterson",
            "crm_source": "cube_acr",
        },
    }
    deal = {
        "id": "d-paid",
        "contact_id": "paid-1",
        "properties": {"dealstage": STAGE["paid"], "dealname": "Kyle Peterson"},
    }
    ev = Engagement(
        source="cube_acr",
        external_id="paid-call",
        email="kyle@petersonroofs.com",
        phone="+15551230000",
        first_name="Kyle",
        last_name="Peterson",
        name="Kyle Peterson",
        company="Roofs by Peterson",
        transcript=("Talking about the campaign and leads in SalesGlider this week. " * 8),
        raw_subject="Kyle Peterson campaign check-in",
        extra={"transcript_kind": "docx_transcript"},
    )
    hs = FakeHubSpot([contact])
    hs.deals = [deal]
    report = CycleReport()
    _handle_engagement(ev, make_settings(), hs, Memory(make_settings(), data_dir=tmp_path), None, report)
    assert [d["id"] for d in hs.deals] == ["d-paid"]
    assert hs.deals[0]["properties"]["dealstage"] == STAGE["paid"]
    assert not any(w[0] == "upsert_deal" for w in hs.writes)
    assert hs.notes


def test_partner_vendor_call_creates_no_deal(tmp_path):
    ev = Engagement(
        source="cube_acr",
        external_id="vendor-1",
        email="seth@seopartner.com",
        first_name="Seth",
        last_name="Kingdon",
        name="Seth Kingdon",
        company="SEO Partner",
        transcript=("SEO partner sync on vendor deliverables and the next report. " * 8),
        raw_subject="Seth Kingdon partner sync",
        extra={"transcript_kind": "docx_transcript"},
    )
    assert not cube_has_sales_intent(ev)
    assert not is_cube_business_discovery(ev)
    hs = FakeHubSpot()
    report = CycleReport()
    _handle_engagement(ev, make_settings(), hs, Memory(make_settings(), data_dir=tmp_path), None, report)
    assert hs.deals == []
    assert not any(w[0] == "upsert_deal" for w in hs.writes)


def test_unlisted_client_crm_source_fireflies_routine_call_creates_no_deal(tmp_path):
    contact = {
        "id": "sam-1",
        "properties": {
            "email": "sam@acme.com",
            "firstname": "Sam",
            "lastname": "River",
            "phone": "+15559870000",
            "crm_source": "fireflies",
        },
    }
    ev = Engagement(
        source="cube_acr",
        external_id="routine-1",
        email="sam@acme.com",
        phone="+15559870000",
        first_name="Sam",
        last_name="River",
        name="Sam River",
        transcript=("Quick catch-up on how things are going this week and next. " * 8),
        raw_subject="Sam River 1:1",
        extra={"transcript_kind": "docx_transcript"},
    )
    assert not contact_is_prospect(contact, [])
    assert not is_cube_business_discovery(ev)
    hs = FakeHubSpot([contact])
    report = CycleReport()
    _handle_engagement(ev, make_settings(), hs, Memory(make_settings(), data_dir=tmp_path), None, report)
    assert hs.deals == []
    assert not any(w[0] == "upsert_deal" for w in hs.writes)


def test_supabase_freshness_fail_skips_cube_backfill(tmp_path, monkeypatch):
    settings = make_settings(
        dry_run=True,
        hubspot_token="tok",
        google_api_key="k",
        gmail_refresh_token="r",
        supabase_url="https://example.supabase.co",
        supabase_key="service-role",
    )
    memory = Memory(settings, data_dir=tmp_path)
    assert memory.use_supabase

    def boom(*_a, **_k):
        raise RuntimeError("supabase down")

    monkeypatch.setattr(memory, "_sb_schema", boom)
    seen = {"backfill": None}

    def fake_scan(*_a, **k):
        seen["backfill"] = k.get("backfill")
        return [_sales_cube(0)]

    class Snap:
        upcoming = set()
        recent = set()
        create_engagements = []
        events = []
        calendar_api_ok = True
        calendar_api_error = ""

        def protect_emails(self):
            return set()

    monkeypatch.setattr("crmbrain.cycle.HubSpot", lambda settings: FakeHubSpot())
    monkeypatch.setattr("crmbrain.cycle.Memory", lambda settings: memory)
    monkeypatch.setattr("crmbrain.cycle.Gmail", lambda settings: object())
    monkeypatch.setattr("crmbrain.cycle.calendar_events.load_calendar", lambda *a, **k: Snap())
    monkeypatch.setattr("crmbrain.cycle.has_drive_access", lambda *a, **k: True)
    monkeypatch.setattr("crmbrain.cycle.cube_acr.scan", fake_scan)
    monkeypatch.setattr("crmbrain.cycle.fireflies.scan", lambda *a, **k: [])
    monkeypatch.setattr("crmbrain.cycle.smartlead.scan", lambda *a, **k: [])
    monkeypatch.setattr("crmbrain.cycle.rvm.scan", lambda *a, **k: [])
    monkeypatch.setattr("crmbrain.cycle.gmail_scan.scan_people", lambda *a, **k: [])
    monkeypatch.setattr("crmbrain.cycle.gmail_scan.scan", lambda *a, **k: [])
    report = cycle_run(settings)
    assert seen["backfill"] is False
    assert any("freshness" in w.lower() and "skip backfill" in w.lower() for w in report.warnings)


def test_scan_falls_back_to_next_ranked_copy(monkeypatch):
    items = [
        cube_acr.DriveFile(
            file_id="winner-docx",
            name=TYLER_TITLE,
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            modified_time=datetime(2026, 9, 30, 19, 5, tzinfo=timezone.utc),
            md5="md5-docx",
            folder_date="2026-09-30",
        ),
        cube_acr.DriveFile(
            file_id="fallback-gdoc",
            name=TYLER_TITLE.replace(" - transcript.docx", ""),
            mime_type="application/vnd.google-apps.document",
            modified_time=datetime(2026, 9, 30, 19, tzinfo=timezone.utc),
            md5="md5-gdoc",
            folder_date="2026-09-30",
        ),
    ]

    def load(_auth, item, kind):
        if item.file_id == "winner-docx":
            raise RuntimeError("download failed")
        return "SalesGlider discovery call about pricing and the monthly retainer. " * 8

    monkeypatch.setattr(cube_acr, "resolve_drive_auth", lambda settings: DriveAuth("api_key", {}, "k"))
    monkeypatch.setattr(cube_acr, "list_transcript_candidates", lambda *a, **k: items)
    monkeypatch.setattr(cube_acr, "_load_text", load)
    evs = cube_acr.scan(make_settings(cube_folder=ROOT, google_api_key="k"))
    assert [e.external_id for e in evs] == ["fallback-gdoc"]
    assert evs[0].extra.get("transcript_kind") == "gdoc"
