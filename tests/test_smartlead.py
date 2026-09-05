from datetime import datetime, timedelta, timezone

import requests

from crmbrain.config import Settings
from crmbrain.cycle import cycle_status, run as cycle_run
from crmbrain.memory import Memory
from crmbrain.models import CycleReport
from crmbrain.sources import smartlead


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


class CycleHubSpot:
    def ensure_properties(self):
        return None

    def find_contact(self, **_k):
        return None

    def iter_contacts(self, _properties):
        return iter(())


class FakeResp:
    def __init__(self, status, payload=None, headers=None, text=""):
        self.status_code = status
        self._payload = {} if payload is None else payload
        self.headers = headers or {}
        self.reason = "Too Many Requests" if status == 429 else "Error"
        self.text = text or ("" if status < 400 else f"{status} {self.reason}")
        self.url = "https://server.smartlead.ai/test"

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(
                f"{self.status_code} Client Error: {self.reason} for url: {self.url}",
                response=self,
            )


def _lead_row(lead_id=11, email="pat@acme.com"):
    return {
        "campaign_lead_map_id": f"map-{lead_id}",
        "lead": {"id": lead_id, "email": email, "first_name": "Pat", "last_name": "Lee"},
    }


def test_get_retries_429_then_200(monkeypatch):
    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append((url, params))
        if len(calls) == 1:
            return FakeResp(429, headers={"Retry-After": "1.5"})
        return FakeResp(200, {"ok": True})

    slept = []
    monkeypatch.setattr(smartlead, "_sleep", slept.append)
    monkeypatch.setattr(smartlead.requests, "get", fake_get)

    settings = make_settings(smartlead_key="sl-key")
    assert smartlead._get(settings, "api/v1/campaigns/3739758/leads") == {"ok": True}
    assert len(calls) == 2
    assert slept == [1.5]


def test_get_retries_503_with_jitter_backoff(monkeypatch):
    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append(url)
        if len(calls) < 3:
            return FakeResp(503)
        return FakeResp(200, {"data": []})

    slept = []
    monkeypatch.setattr(smartlead.random, "random", lambda: 0.5)
    monkeypatch.setattr(smartlead, "_sleep", slept.append)
    monkeypatch.setattr(smartlead.requests, "get", fake_get)

    settings = make_settings(smartlead_key="sl-key")
    assert smartlead._get(settings, "api/v1/campaigns") == {"data": []}
    assert len(calls) == 3
    assert slept == [1.0, 2.0]


def test_get_honors_http_date_retry_after(monkeypatch):
    when = datetime.now(timezone.utc) + timedelta(seconds=7)
    header = when.strftime("%a, %d %b %Y %H:%M:%S GMT")

    def fake_get(url, params=None, timeout=None):
        if not hasattr(fake_get, "n"):
            fake_get.n = 0
        fake_get.n += 1
        if fake_get.n == 1:
            return FakeResp(429, headers={"Retry-After": header})
        return FakeResp(200, {"ok": 1})

    slept = []
    monkeypatch.setattr(smartlead, "_sleep", slept.append)
    monkeypatch.setattr(smartlead.requests, "get", fake_get)

    smartlead._get(make_settings(smartlead_key="k"), "api/v1/campaigns")
    assert slept and 5 <= slept[0] <= 8


def test_get_exhausted_429_raises(monkeypatch):
    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append(url)
        return FakeResp(429, text="Too Many Requests")

    monkeypatch.setattr(smartlead, "_sleep", lambda _s: None)
    monkeypatch.setattr(smartlead.requests, "get", fake_get)

    try:
        smartlead._get(make_settings(smartlead_key="k"), "api/v1/campaigns/3739758/leads")
        raise AssertionError("expected HTTPError")
    except requests.HTTPError as exc:
        assert "429" in str(exc)
    assert len(calls) == smartlead.MAX_RETRIES + 1


def test_scan_continues_after_one_campaign_exhausted(monkeypatch):
    lead_hits = {3739758: 0, 99: 0}

    def fake_get(settings, path, params=None):
        if path == "api/v1/leads/fetch-categories":
            return [{"id": 1, "name": "Interested", "sentiment_type": "positive"}]
        if path == "api/v1/campaigns":
            return [
                {"id": 3739758, "name": "Camp A", "status": "ACTIVE"},
                {"id": 99, "name": "Camp B", "status": "ACTIVE"},
            ]
        if path == "api/v1/campaigns/3739758/leads":
            lead_hits[3739758] += 1
            raise requests.HTTPError("429 Client Error: Too Many Requests", response=FakeResp(429))
        if path == "api/v1/campaigns/99/leads":
            lead_hits[99] += 1
            return {"data": [_lead_row(22, "bob@ok.com")]}
        if path.endswith("/message-history"):
            return {"history": []}
        raise AssertionError(path)

    monkeypatch.setattr(smartlead, "_get", fake_get)
    monkeypatch.setattr(smartlead, "_sleep", lambda _s: None)

    errors: list[str] = []
    events = smartlead.scan(make_settings(smartlead_key="sl"), errors=errors)
    assert lead_hits[3739758] == 1
    assert lead_hits[99] == 1
    assert len(events) == 1
    assert events[0].email == "bob@ok.com"
    assert events[0].extra["campaign_id"] == 99
    assert len(errors) == 1
    assert "3739758" in errors[0]
    assert "429" in errors[0]


def test_scan_recovered_429_not_recorded_as_error(monkeypatch):
    states = {"leads": 0}

    def fake_requests_get(url, params=None, timeout=None):
        if url.endswith("/api/v1/leads/fetch-categories"):
            return FakeResp(200, [{"id": 1, "name": "Interested", "sentiment_type": "positive"}])
        if url.endswith("/api/v1/campaigns"):
            return FakeResp(200, [{"id": 3739758, "name": "Camp A", "status": "ACTIVE"}])
        if url.endswith("/api/v1/campaigns/3739758/leads"):
            states["leads"] += 1
            if states["leads"] == 1:
                return FakeResp(429, headers={"Retry-After": "0.1"})
            return FakeResp(200, {"data": [_lead_row()]})
        if url.endswith("/message-history"):
            return FakeResp(200, {"history": []})
        raise AssertionError(url)

    monkeypatch.setattr(smartlead, "_sleep", lambda _s: None)
    monkeypatch.setattr(smartlead.requests, "get", fake_requests_get)

    errors: list[str] = []
    events = smartlead.scan(make_settings(smartlead_key="sl"), errors=errors)
    assert states["leads"] == 2
    assert len(events) == 1
    assert events[0].email == "pat@acme.com"
    assert errors == []


def test_scan_pauses_between_pages_and_campaigns(monkeypatch):
    monkeypatch.setattr(smartlead, "LEADS_PAGE_SIZE", 1)
    monkeypatch.setattr(smartlead, "PAGE_PAUSE", 0.25)
    monkeypatch.setattr(smartlead, "CAMPAIGN_PAUSE", 0.35)
    slept = []

    pages = {
        1: [_lead_row(1, "one@a.com"), _lead_row(2, "two@a.com")],
        2: [_lead_row(3, "three@b.com")],
    }
    offsets = {1: 0, 2: 0}

    def fake_get(settings, path, params=None):
        if path == "api/v1/leads/fetch-categories":
            return [{"id": 1, "name": "Interested", "sentiment_type": "positive"}]
        if path == "api/v1/campaigns":
            return [
                {"id": 1, "name": "A", "status": "ACTIVE"},
                {"id": 2, "name": "B", "status": "ACTIVE"},
            ]
        if path.endswith("/leads"):
            cid = 1 if "/campaigns/1/" in path else 2
            offset = int((params or {}).get("offset") or 0)
            rows = pages[cid][offset : offset + 1]
            offsets[cid] = offset
            return {"data": rows}
        if path.endswith("/message-history"):
            return {"history": []}
        raise AssertionError(path)

    monkeypatch.setattr(smartlead, "_get", fake_get)
    monkeypatch.setattr(smartlead, "_sleep", slept.append)

    events = smartlead.scan(make_settings(smartlead_key="sl"), errors=[])
    assert [e.email for e in events] == ["one@a.com", "two@a.com", "three@b.com"]
    assert 0.25 in slept
    assert 0.35 in slept


def test_cycle_status_ok_unless_data_skipped():
    assert cycle_status(CycleReport()) == "ok"
    recovered = CycleReport()
    recovered.errors.append("smartlead campaign 3739758: 429 recovered after retry")
    assert cycle_status(recovered) == "ok"
    skipped = CycleReport()
    skipped.errors.append("smartlead campaign 3739758: 429 Client Error: Too Many Requests")
    assert cycle_status(skipped) == "partial"


def test_cycle_ok_when_smartlead_429_then_200(tmp_path, monkeypatch):
    settings = make_settings(hubspot_token="tok", smartlead_key="sl")
    monkeypatch.setattr("crmbrain.cycle.Memory", lambda s: Memory(s, data_dir=tmp_path))
    monkeypatch.setattr("crmbrain.cycle.HubSpot", lambda s: CycleHubSpot())
    monkeypatch.setattr("crmbrain.cycle.cube_acr.scan", lambda s: [])
    monkeypatch.setattr("crmbrain.cycle.fireflies.scan", lambda s: [])
    monkeypatch.setattr("crmbrain.cycle.rvm.scan", lambda s: [])
    monkeypatch.setattr("crmbrain.cycle.allo.scan", lambda s, g: [])
    monkeypatch.setattr("crmbrain.cycle.prune.run", lambda hs, report: None)
    monkeypatch.setattr("crmbrain.cycle._fire_ticker", lambda *a, **k: None)
    monkeypatch.setattr(smartlead, "_sleep", lambda _s: None)

    hits = {"leads": 0}

    def fake_requests_get(url, params=None, timeout=None):
        if url.endswith("/api/v1/leads/fetch-categories"):
            return FakeResp(200, [{"id": 1, "name": "Interested", "sentiment_type": "positive"}])
        if url.endswith("/api/v1/campaigns"):
            return FakeResp(200, [{"id": 3739758, "name": "Camp A", "status": "ACTIVE"}])
        if url.endswith("/api/v1/campaigns/3739758/leads"):
            hits["leads"] += 1
            if hits["leads"] == 1:
                return FakeResp(429)
            return FakeResp(200, {"data": []})
        raise AssertionError(url)

    monkeypatch.setattr(smartlead.requests, "get", fake_requests_get)
    report = cycle_run(settings)
    assert hits["leads"] == 2
    assert report.errors == []
    assert cycle_status(report) == "ok"
    assert any(r.get("status") == "ok" for r in Memory(settings, data_dir=tmp_path)._local.get("runs", []))


def test_cycle_partial_when_one_campaign_exhausted(tmp_path, monkeypatch):
    settings = make_settings(hubspot_token="tok", smartlead_key="sl")
    monkeypatch.setattr("crmbrain.cycle.Memory", lambda s: Memory(s, data_dir=tmp_path))
    monkeypatch.setattr("crmbrain.cycle.HubSpot", lambda s: CycleHubSpot())
    monkeypatch.setattr("crmbrain.cycle.cube_acr.scan", lambda s: [])
    monkeypatch.setattr("crmbrain.cycle.fireflies.scan", lambda s: [])
    monkeypatch.setattr("crmbrain.cycle.rvm.scan", lambda s: [])
    monkeypatch.setattr("crmbrain.cycle.allo.scan", lambda s, g: [])
    monkeypatch.setattr("crmbrain.cycle.prune.run", lambda hs, report: None)
    monkeypatch.setattr("crmbrain.cycle._fire_ticker", lambda *a, **k: None)
    monkeypatch.setattr(smartlead, "_sleep", lambda _s: None)

    def fake_get(settings, path, params=None):
        if path == "api/v1/leads/fetch-categories":
            return [{"id": 1, "name": "Interested", "sentiment_type": "positive"}]
        if path == "api/v1/campaigns":
            return [
                {"id": 3739758, "name": "Camp A", "status": "ACTIVE"},
                {"id": 99, "name": "Camp B", "status": "STARTED"},
            ]
        if path == "api/v1/campaigns/3739758/leads":
            raise requests.HTTPError("429 Client Error: Too Many Requests", response=FakeResp(429))
        if path == "api/v1/campaigns/99/leads":
            return {"data": [_lead_row(5, "ok@acme.com")]}
        if path.endswith("/message-history"):
            return {"history": []}
        raise AssertionError(path)

    monkeypatch.setattr(smartlead, "_get", fake_get)
    report = cycle_run(settings)
    assert any("3739758" in e and "429" in e for e in report.errors)
    assert not any("99" in e for e in report.errors)
    assert cycle_status(report) == "partial"
