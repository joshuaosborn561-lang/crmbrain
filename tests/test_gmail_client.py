from datetime import datetime, timedelta, timezone

import requests

from crmbrain.config import Settings
from crmbrain.cycle import cycle_status
from crmbrain.gmail_client import (
    MAX_READ_RETRIES,
    READ_TIMEOUT,
    Gmail,
    _retry_after_seconds,
)
from crmbrain.models import CycleReport
from crmbrain.sources import gmail_scan


def make_settings(**kwargs) -> Settings:
    base = dict(
        hubspot_token="tok",
        gmail_client_id="cid",
        gmail_client_secret="csecret",
        gmail_refresh_token="rtoken",
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


class FakeResp:
    def __init__(self, status, payload=None, headers=None, text=""):
        self.status_code = status
        self._payload = {} if payload is None else payload
        self.headers = headers or {}
        self.reason = "Too Many Requests" if status == 429 else "Error"
        self.text = text or ("" if status < 400 else f"{status} {self.reason}")
        self.url = "https://gmail.googleapis.com/gmail/v1/users/me/messages"

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(
                f"{self.status_code} Client Error: {self.reason} for url: {self.url}",
                response=self,
            )


def _gmail() -> Gmail:
    client = Gmail(make_settings())
    client._token = "access-token"
    return client


def test_search_retries_read_timeout_then_succeeds(monkeypatch):
    gmail = _gmail()
    slept = []
    monkeypatch.setattr("crmbrain.gmail_client._sleep", slept.append)
    monkeypatch.setattr("crmbrain.gmail_client.random.random", lambda: 0.5)
    calls = {"n": 0}

    def fake_request(method, url, timeout=None, **kwargs):
        calls["n"] += 1
        assert timeout == READ_TIMEOUT
        if calls["n"] == 1:
            raise requests.exceptions.ReadTimeout(
                "HTTPSConnectionPool(host='gmail.googleapis.com', port=443): Read timed out. (read timeout=30)"
            )
        return FakeResp(200, {"messages": [{"id": "m1"}]})

    gmail.session.request = fake_request
    assert gmail.search("newer_than:2d") == [{"id": "m1"}]
    assert calls["n"] == 2
    assert slept == [1.0]


def test_search_timeout_exhausted_still_raises(monkeypatch):
    gmail = _gmail()
    monkeypatch.setattr("crmbrain.gmail_client._sleep", lambda _s: None)
    calls = {"n": 0}

    def always_timeout(method, url, timeout=None, **kwargs):
        calls["n"] += 1
        raise requests.exceptions.ReadTimeout(
            "HTTPSConnectionPool(host='gmail.googleapis.com', port=443): Read timed out. (read timeout=30)"
        )

    gmail.session.request = always_timeout
    try:
        gmail.search("newer_than:2d")
        raise AssertionError("expected ReadTimeout")
    except requests.exceptions.ReadTimeout as exc:
        assert "gmail.googleapis.com" in str(exc)
    assert calls["n"] == MAX_READ_RETRIES + 1


def test_get_retries_429_honors_retry_after(monkeypatch):
    gmail = _gmail()
    slept = []
    monkeypatch.setattr("crmbrain.gmail_client._sleep", slept.append)
    calls = {"n": 0}

    def fake_request(method, url, timeout=None, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeResp(429, headers={"Retry-After": "1.5"})
        return FakeResp(200, {"id": "abc", "snippet": "hi"})

    gmail.session.request = fake_request
    assert gmail.get("abc")["id"] == "abc"
    assert calls["n"] == 2
    assert slept == [1.5]


def test_search_retries_503_with_jitter_backoff(monkeypatch):
    gmail = _gmail()
    slept = []
    monkeypatch.setattr("crmbrain.gmail_client.random.random", lambda: 0.5)
    monkeypatch.setattr("crmbrain.gmail_client._sleep", slept.append)
    calls = {"n": 0}

    def fake_request(method, url, timeout=None, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            return FakeResp(503)
        return FakeResp(200, {"messages": []})

    gmail.session.request = fake_request
    assert gmail.search("in:inbox") == []
    assert calls["n"] == 3
    assert slept == [1.0, 2.0]


def test_retry_after_http_date():
    when = datetime.now(timezone.utc) + timedelta(seconds=7)
    header = when.strftime("%a, %d %b %Y %H:%M:%S GMT")
    resp = FakeResp(429, headers={"Retry-After": header})
    delay = _retry_after_seconds(resp, 99.0)
    assert 5 <= delay <= 8


def test_scan_people_timeout_then_success_stays_ok(monkeypatch):
    """A recovered Gmail stall must not become a cycle error."""
    gmail = _gmail()
    monkeypatch.setattr("crmbrain.gmail_client._sleep", lambda _s: None)
    calls = {"n": 0}

    def fake_request(method, url, timeout=None, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise requests.exceptions.ReadTimeout(
                "HTTPSConnectionPool(host='gmail.googleapis.com', port=443): Read timed out. (read timeout=30)"
            )
        return FakeResp(200, {"messages": []})

    gmail.session.request = fake_request
    report = CycleReport()
    events = gmail_scan.scan_people(make_settings(), gmail)
    assert events == []
    assert calls["n"] >= 2
    assert report.errors == []
    assert cycle_status(report) == "ok"
