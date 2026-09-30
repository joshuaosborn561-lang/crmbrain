"""Cube Drive API v3 listing, personal-number skip, non-deal exclusion, dry-run."""

from datetime import date, datetime, timedelta, timezone
from io import BytesIO

from crmbrain.config import is_non_deal_person, is_personal, now_utc, personal_numbers
from crmbrain.cycle import _handle_engagement, apply_gmail_stage_update, integration_status, run as cycle_run
from crmbrain.gmail_client import CALENDAR_READONLY_SCOPE
from crmbrain.google_auth import CALENDAR_READONLY, DRIVE_READONLY, report_scopes
from crmbrain.hubspot import HubSpot
from crmbrain.memory import Memory
from crmbrain.models import CycleReport, Engagement
from crmbrain.sources import cube_acr, gmail_scan
from crmbrain.sources.cube_acr import (
    CubeAuthError,
    DriveAuth,
    docx_text,
    list_transcript_candidates,
    parse_cube_title,
    resolve_drive_auth,
    should_skip_cube_call,
)
from crmbrain.staleness import WATCHED_SOURCES
from tests.test_crm_gating import FakeHubSpot, make_settings


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
                        "2026-09-29 14:32 +15559876543 Rob Lawson - transcript.docx",
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


def test_parse_cube_title_and_docx_text():
    from docx import Document

    meta = parse_cube_title("2026-09-29 14-32-05 +15559876543 Rob Lawson - transcript.docx")
    assert meta["date"] == "2026-09-29"
    assert meta["phone"].endswith("5559876543")
    assert "Rob Lawson" in meta["name"]
    assert meta["time"]

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
    assert should_skip_cube_call(name="Nonna", phone="+19734613447")
    assert should_skip_cube_call(
        title="2026-09-30 10:00 +19734613447 Nonna - transcript.docx",
        phone="+19734613447",
    )
    assert not should_skip_cube_call(name="Rob Lawson", phone="+15559876543")


def test_scan_skips_personal_number_and_dedupes_md5(monkeypatch):
    items = [
        cube_acr.DriveFile(
            file_id=LATE_FILE,
            name="2026-09-29 14:32 +15559876543 Rob Lawson - transcript.docx",
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            modified_time=datetime(2026, 9, 29, 19, tzinfo=timezone.utc),
            md5="same-md5",
            folder_date="2026-09-29",
        ),
        cube_acr.DriveFile(
            file_id=DUP_FILE,
            name="2026-09-29 14:32 +15559876543 Rob Lawson - transcript.docx",
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            modified_time=datetime(2026, 9, 29, 19, 5, tzinfo=timezone.utc),
            md5="same-md5",
            folder_date="2026-09-29",
        ),
        cube_acr.DriveFile(
            file_id="nonna-file",
            name="2026-09-30 09:00 +19734613447 Nonna - transcript.docx",
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            modified_time=datetime(2026, 9, 30, 14, tzinfo=timezone.utc),
            md5="nonna-md5",
            folder_date="2026-09-30",
        ),
    ]

    monkeypatch.setattr(cube_acr, "resolve_drive_auth", lambda settings: DriveAuth("api_key", {}, "k"))
    monkeypatch.setattr(cube_acr, "list_transcript_candidates", lambda *a, **k: items)
    monkeypatch.setattr(
        cube_acr,
        "_load_text",
        lambda auth, item, kind: "SalesGlider discovery call about their roofing pipeline and owners.",
    )
    evs = cube_acr.scan(make_settings(cube_folder=ROOT, google_api_key="k"))
    assert [e.external_id for e in evs] == [LATE_FILE]
    assert evs[0].phone.endswith("5559876543")
    assert evs[0].display_name() == "Rob Lawson"


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
