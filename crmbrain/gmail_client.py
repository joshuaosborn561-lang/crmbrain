from __future__ import annotations

import base64
import logging
import random
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime
from typing import Any

import requests

from crmbrain.config import Settings
from crmbrain.google_auth import CALENDAR_READONLY

logger = logging.getLogger(__name__)

# Calendar listing uses Calendar API v3 when the refresh token has calendar.readonly.
CALENDAR_READONLY_SCOPE = CALENDAR_READONLY

# Listing/scanning messages can stall past 30s; retry transient reads.
READ_TIMEOUT = 45
WRITE_TIMEOUT = 30
MAX_READ_RETRIES = 3
BACKOFF_BASE = 1.0
BACKOFF_CAP = 16.0
RETRYABLE_STATUS = frozenset({429, 503})
GMAIL_RATE_LIMIT_REASONS = frozenset(
    {
        "ratelimitexceeded",
        "userratelimitexceeded",
        "userratelimitexceededunreg",
        "quotaexceeded",
        "dailylimitexceeded",
        "resource_exhausted",
        "resourceexhausted",
    }
)


def _sleep(seconds: float) -> None:
    if seconds > 0:
        time.sleep(seconds)


def _retry_after_seconds(resp: requests.Response, fallback: float) -> float:
    raw = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
    if raw is None:
        return fallback
    raw = str(raw).strip()
    if not raw:
        return fallback
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return fallback


def _gmail_error_reason(resp: requests.Response) -> str:
    try:
        payload = resp.json()
    except ValueError:
        payload = {}
    err = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(err, dict):
        for item in err.get("errors") or []:
            reason = str((item or {}).get("reason") or "").strip()
            if reason:
                return reason
        status = str(err.get("status") or "").strip()
        if status:
            return status
        return str(err.get("message") or "").strip()
    if isinstance(err, str):
        return err
    return (resp.text or "")[:240]


def _normalize_reason(reason: str) -> str:
    return "".join(ch for ch in (reason or "").lower() if ch.isalnum())


def is_gmail_rate_limit(resp: requests.Response) -> bool:
    """Gmail often returns 403 + userRateLimitExceeded for concurrent quota, not 429."""
    if resp.status_code == 429:
        return True
    if resp.status_code != 403:
        return False
    reason = _normalize_reason(_gmail_error_reason(resp))
    if reason in GMAIL_RATE_LIMIT_REASONS or any(r in reason for r in GMAIL_RATE_LIMIT_REASONS):
        return True
    blob = f"{_gmail_error_reason(resp)} {resp.text or ''}".lower()
    return "rate limit" in blob or "too many" in blob or "quota exceeded" in blob


def is_gmail_scope_error(resp: requests.Response) -> bool:
    if resp.status_code != 403 or is_gmail_rate_limit(resp):
        return False
    blob = f"{_gmail_error_reason(resp)} {resp.text or ''}".lower()
    return any(
        token in blob
        for token in (
            "insufficientpermissions",
            "insufficient permission",
            "access_token_scope_insufficient",
            "access not granted",
            "requiredaccessnotgranted",
            "insufficient authentication scopes",
        )
    )


def _backoff_with_jitter(attempt: int) -> float:
    base = min(BACKOFF_CAP, BACKOFF_BASE * (2**attempt))
    return min(BACKOFF_CAP, base * (0.5 + random.random()))


class Gmail:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._token = ""
        self.session = requests.Session()

    def token(self) -> str:
        if self._token:
            return self._token
        resp = self._request(
            "POST",
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": self.settings.gmail_client_id,
                "client_secret": self.settings.gmail_client_secret,
                "refresh_token": self.settings.gmail_refresh_token,
                "grant_type": "refresh_token",
            },
            retry=True,
            timeout=READ_TIMEOUT,
        )
        resp.raise_for_status()
        self._token = resp.json()["access_token"]
        return self._token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token()}"}

    def _request(
        self,
        method: str,
        url: str,
        *,
        retry: bool = False,
        timeout: int | None = None,
        **kwargs: Any,
    ) -> requests.Response:
        """Gmail HTTP. Reads retry timeouts/429/503 so one stall is not fatal."""
        timeout = READ_TIMEOUT if timeout is None else timeout
        attempts = MAX_READ_RETRIES + 1 if retry else 1
        last_exc: BaseException | None = None
        for attempt in range(attempts):
            try:
                resp = self.session.request(method, url, timeout=timeout, **kwargs)
            except requests.Timeout as exc:
                last_exc = exc
                if attempt + 1 >= attempts:
                    raise
                delay = _backoff_with_jitter(attempt)
                logger.warning(
                    "gmail %s %s timed out, retry %s/%s in %.2fs",
                    method,
                    url,
                    attempt + 1,
                    MAX_READ_RETRIES,
                    delay,
                )
                _sleep(delay)
                continue
            rate_limited = is_gmail_rate_limit(resp)
            if retry and (resp.status_code in RETRYABLE_STATUS or rate_limited) and attempt + 1 < attempts:
                delay = min(BACKOFF_CAP, _retry_after_seconds(resp, _backoff_with_jitter(attempt)))
                logger.warning(
                    "gmail %s %s HTTP %s (%s), retry %s/%s in %.2fs",
                    method,
                    url,
                    resp.status_code,
                    _gmail_error_reason(resp) or resp.reason,
                    attempt + 1,
                    MAX_READ_RETRIES,
                    delay,
                )
                _sleep(delay)
                continue
            if is_gmail_scope_error(resp):
                logger.error(
                    "gmail %s %s HTTP 403 missing scope: %s",
                    method,
                    url,
                    _gmail_error_reason(resp) or resp.text[:200],
                )
            return resp
        raise last_exc or RuntimeError("gmail request failed")

    def search(self, query: str, max_results: int = 50) -> list[dict]:
        resp = self._request(
            "GET",
            "https://gmail.googleapis.com/gmail/v1/users/me/messages",
            headers=self._headers(),
            params={"q": query, "maxResults": max_results},
            retry=True,
            timeout=READ_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json().get("messages", [])

    def get(self, message_id: str) -> dict[str, Any]:
        resp = self._request(
            "GET",
            f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}",
            headers=self._headers(),
            params={"format": "full"},
            retry=True,
            timeout=READ_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()

    def headers_map(self, message: dict) -> dict[str, str]:
        headers = {}
        for item in message.get("payload", {}).get("headers", []):
            headers[item["name"].lower()] = item.get("value", "")
        return headers

    def body_text(self, message: dict) -> str:
        chunks: list[str] = []

        def walk(part: dict) -> None:
            data = (part.get("body") or {}).get("data")
            mime = part.get("mimeType") or ""
            if data and ("text/plain" in mime or "html" in mime):
                raw = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
                chunks.append(raw)
            for child in part.get("parts") or []:
                walk(child)

        walk(message.get("payload") or {})
        return "\n".join(chunks)

    def calendar_parts(self, message: dict) -> list[str]:
        """ICS / text/calendar parts on a Gmail message."""
        chunks: list[str] = []

        def walk(part: dict) -> None:
            data = (part.get("body") or {}).get("data")
            mime = (part.get("mimeType") or "").lower()
            filename = (part.get("filename") or "").lower()
            if data and ("calendar" in mime or filename.endswith(".ics")):
                raw = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
                chunks.append(raw)
            for child in part.get("parts") or []:
                walk(child)

        walk(message.get("payload") or {})
        return chunks

    def list_calendar_events(self, time_min: datetime, time_max: datetime) -> list[dict]:
        """Primary calendar events. Empty when the token has no calendar scope."""
        params = {
            "timeMin": time_min.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "timeMax": time_max.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": 250,
        }
        resp = self._request(
            "GET",
            "https://www.googleapis.com/calendar/v3/calendars/primary/events",
            headers=self._headers(),
            params=params,
            retry=True,
            timeout=READ_TIMEOUT,
        )
        if resp.status_code in {401, 403}:
            logger.info("calendar api %s — falling back to Gmail invites", resp.status_code)
            raise PermissionError(
                f"calendar api {resp.status_code} — grant Calendar readonly scope "
                f"on the Gmail OAuth token ({CALENDAR_READONLY_SCOPE})"
            )
        resp.raise_for_status()
        return resp.json().get("items") or []

    def send(self, to: str, subject: str, body: str) -> None:
        msg = MIMEText(body)
        msg["to"] = to
        msg["from"] = "joshua@salesglidergrowth.com"
        msg["subject"] = subject
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        resp = self._request(
            "POST",
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            headers={**self._headers(), "Content-Type": "application/json"},
            json={"raw": raw},
            timeout=WRITE_TIMEOUT,
        )
        resp.raise_for_status()
