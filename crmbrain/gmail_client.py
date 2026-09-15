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

logger = logging.getLogger(__name__)

# Listing/scanning messages can stall past 30s; retry transient reads.
READ_TIMEOUT = 45
WRITE_TIMEOUT = 30
MAX_READ_RETRIES = 3
BACKOFF_BASE = 1.0
BACKOFF_CAP = 16.0
RETRYABLE_STATUS = frozenset({429, 503})


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
            if retry and resp.status_code in RETRYABLE_STATUS and attempt + 1 < attempts:
                delay = min(BACKOFF_CAP, _retry_after_seconds(resp, _backoff_with_jitter(attempt)))
                logger.warning(
                    "gmail %s %s HTTP %s, retry %s/%s in %.2fs",
                    method,
                    url,
                    resp.status_code,
                    attempt + 1,
                    MAX_READ_RETRIES,
                    delay,
                )
                _sleep(delay)
                continue
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
