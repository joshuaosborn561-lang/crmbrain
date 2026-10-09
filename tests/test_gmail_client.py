from datetime import datetime, timedelta, timezone

import requests

from crmbrain.config import Settings
from crmbrain.cycle import cycle_status
from crmbrain.gmail_client import (
    MAX_READ_RETRIES,
    READ_TIMEOUT,
    Gmail,
    GmailRateLimitError,
    _retry_after_seconds,
    header_has_contact_email,
    is_gmail_rate_limit,
    is_gmail_rate_limit_exc,
    is_gmail_scope_error,
    should_skip_nurture_thread,
    subject_is_calendar_noise,
    subject_is_scheduling_only,
    subject_is_tj_thread,
    subject_names_other_meeting_guest,
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


def test_search_retries_403_user_rate_limit_then_succeeds(monkeypatch):
    gmail = _gmail()
    slept = []
    monkeypatch.setattr("crmbrain.gmail_client._sleep", slept.append)
    calls = {"n": 0}
    payload = {
        "error": {
            "code": 403,
            "message": "User-rate limit exceeded",
            "errors": [{"reason": "userRateLimitExceeded", "message": "User-rate limit exceeded"}],
            "status": "RESOURCE_EXHAUSTED",
        }
    }

    def fake_request(method, url, timeout=None, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeResp(403, payload, text="User-rate limit exceeded")
        return FakeResp(200, {"messages": [{"id": "m1"}]})

    gmail.session.request = fake_request
    assert gmail.search("after:2026/09/18 in:inbox -category:promotions") == [{"id": "m1"}]
    assert calls["n"] == 2
    assert slept


def test_search_403_missing_scope_is_not_retried(monkeypatch):
    gmail = _gmail()
    slept = []
    monkeypatch.setattr("crmbrain.gmail_client._sleep", slept.append)
    calls = {"n": 0}
    payload = {
        "error": {
            "code": 403,
            "message": "Request had insufficient authentication scopes.",
            "status": "PERMISSION_DENIED",
            "details": [{"reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}],
        }
    }

    def fake_request(method, url, timeout=None, **kwargs):
        calls["n"] += 1
        return FakeResp(
            403,
            payload,
            text="Request had insufficient authentication scopes. ACCESS_TOKEN_SCOPE_INSUFFICIENT",
        )

    gmail.session.request = fake_request
    resp = FakeResp(
        403,
        payload,
        text="Request had insufficient authentication scopes. ACCESS_TOKEN_SCOPE_INSUFFICIENT",
    )
    assert is_gmail_rate_limit(resp) is False
    assert is_gmail_scope_error(resp) is True
    try:
        gmail.search("after:2026/09/18 in:inbox")
        raise AssertionError("expected HTTPError")
    except Exception as exc:
        assert "403" in str(exc)
    assert calls["n"] == 1
    assert slept == []


def test_search_exhausted_403_rate_limit_raises(monkeypatch):
    gmail = _gmail()
    monkeypatch.setattr("crmbrain.gmail_client._sleep", lambda _s: None)
    payload = {
        "error": {
            "code": 403,
            "message": "Rate Limit Exceeded",
            "errors": [{"reason": "rateLimitExceeded"}],
        }
    }

    def fake_request(method, url, timeout=None, **kwargs):
        return FakeResp(403, payload, text="rateLimitExceeded")

    gmail.session.request = fake_request
    try:
        gmail.search("after:2026/09/01")
        raise AssertionError("expected GmailRateLimitError")
    except GmailRateLimitError as exc:
        assert "403" in str(exc)
        assert is_gmail_rate_limit_exc(exc)


def test_scan_people_does_not_skip_or_mark_rate_limit_403():
    class PartialGmail:
        def search(self, query, max_results=80):
            return [{"id": "ok-1"}, {"id": "bad-403"}]

        def get(self, mid):
            if mid == "bad-403":
                raise GmailRateLimitError("403 userRateLimitExceeded")
            return {
                "id": mid,
                "internalDate": "1728000000000",
                "snippet": "hello",
                "_headers": {
                    "from": "Pat Lee <pat@clientco.com>",
                    "to": "Joshua <joshua@salesglidergrowth.com>",
                    "subject": "intro",
                },
            }

        def headers_map(self, msg):
            return msg["_headers"]

    settings = make_settings()
    report = CycleReport()
    try:
        gmail_scan.scan_people(settings, PartialGmail(), report=report)
        raise AssertionError("expected GmailRateLimitError")
    except GmailRateLimitError:
        pass
    assert not any("bad-403" in s for s in report.skipped)
    assert not any("bad-403" in w for w in report.warnings)


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


def _msg(message_id, thread_id, subject, frm, to):
    return {
        "id": message_id,
        "threadId": thread_id,
        "payload": {
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "From", "value": frm},
                {"name": "To", "value": to},
                {"name": "Message-ID", "value": f"<{message_id}@mail>"},
            ]
        },
    }


def test_find_contact_thread_skips_calendar_other_person_and_missing_email(monkeypatch):
    """Item 3: skip calendar / scheduling / other-person recaps; require From/To email."""
    assert subject_is_calendar_noise("Accepted: Intro with Josh")
    assert subject_is_calendar_noise("Declined: SalesGlider Intro")
    assert subject_is_calendar_noise("Invitation: Call tomorrow")
    assert subject_is_calendar_noise("Updated invitation: Weekly")
    assert subject_is_calendar_noise("Canceled: Intro")
    assert subject_is_scheduling_only("Call at 10:30am")
    assert subject_is_scheduling_only("Re: meeting today")
    assert subject_names_other_meeting_guest(
        "Your meeting recap - Lionel Francis and Joshua Osborn",
        "Bradley Lord",
    )
    assert not subject_names_other_meeting_guest(
        "Your meeting recap - Lionel Francis and Joshua Osborn",
        "Lionel Francis",
    )
    assert not header_has_contact_email(
        {"from": "josh@salesglidergrowth.com", "to": "list@salesglidergrowth.com"},
        "mike@dolan.test",
    )
    assert should_skip_nurture_thread(
        {
            "subject": "Accepted: Intro",
            "from": "calendar-notification@google.com",
            "to": "mike@dolan.test",
        },
        "mike@dolan.test",
        "Mike Dolan",
    )

    gmail = _gmail()
    inbox = [
        _msg(
            "m-cal",
            "th-cal",
            "Accepted: Intro with Josh",
            "calendar-notification@google.com",
            "mike@dolan.test",
        ),
        _msg(
            "m-sched",
            "th-sched",
            "Call at 10:30am",
            "mike@dolan.test",
            "joshua@salesglidergrowth.com",
        ),
        _msg(
            "m-recap",
            "th-recap",
            "Your meeting recap - Lionel Francis and Joshua Osborn",
            "fred@fireflies.ai",
            "joshua@salesglidergrowth.com",
        ),
        _msg(
            "m-blast",
            "th-blast",
            "Re: 24 new referrals",
            "josh@salesglidergrowth.com",
            "team@salesglidergrowth.com",
        ),
        _msg(
            "m-good",
            "th-good",
            "Kelly Roofing intro",
            "mike@dolan.test",
            "joshua@salesglidergrowth.com",
        ),
    ]

    def fake_search(query, max_results=50):
        del query, max_results
        return [{"id": m["id"], "threadId": m["threadId"]} for m in inbox]

    def fake_get(message_id):
        return next(m for m in inbox if m["id"] == message_id)

    monkeypatch.setattr(gmail, "search", fake_search)
    monkeypatch.setattr(gmail, "get", fake_get)
    found = gmail.find_contact_thread("mike@dolan.test", name="Mike Dolan")
    assert found is not None
    assert found["thread_id"] == "th-good"
    assert found["original_subject"] == "Kelly Roofing intro"

    missing = gmail.find_contact_thread("lionel@francis.test", name="Lionel Francis")
    assert missing is None


def test_kevin_hagemoser_skips_tj_thread(monkeypatch):
    """Item 5: Kevin must not land on a TJ said... thread."""
    assert subject_is_tj_thread("Re: TJ said we should reconnect")
    assert should_skip_nurture_thread(
        {
            "subject": "Re: TJ said...",
            "from": "kevin@hag.test",
            "to": "joshua@salesglidergrowth.com",
        },
        "kevin@hag.test",
        "Kevin Hagemoser",
    )
    gmail = _gmail()
    inbox = [
        _msg(
            "m-tj",
            "th-tj",
            "Re: TJ said we should reconnect",
            "kevin@hag.test",
            "joshua@salesglidergrowth.com",
        ),
        _msg(
            "m-real",
            "th-kevin",
            "Hagemoser follow up",
            "kevin@hag.test",
            "joshua@salesglidergrowth.com",
        ),
    ]

    monkeypatch.setattr(
        gmail,
        "search",
        lambda query, max_results=50: [{"id": m["id"], "threadId": m["threadId"]} for m in inbox],
    )
    monkeypatch.setattr(gmail, "get", lambda mid: next(m for m in inbox if m["id"] == mid))
    found = gmail.find_contact_thread("kevin@hag.test", name="Kevin Hagemoser")
    assert found["thread_id"] == "th-kevin"
    assert "TJ" not in found["original_subject"]
