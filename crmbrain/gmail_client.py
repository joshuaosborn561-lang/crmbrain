from __future__ import annotations

import base64
import logging
import random
import re
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime
from typing import Any

import requests

from crmbrain.config import Settings, is_client_context, is_josh_address
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
CALENDAR_NOTIFY_ADDR = "calendar-notification@google.com"
CALENDAR_SUBJECT_PREFIXES = (
    "accepted:",
    "declined:",
    "invitation:",
    "updated invitation",
    "canceled",
    "cancelled",
)
_SCHEDULING_ONLY_RE = re.compile(
    r"^(?:re:\s*)?(?:"
    r"call at\s+\d|"
    r"meeting today|"
    r"meeting tomorrow|"
    r"meet(?:ing)? at\s+\d"
    r")",
    re.I,
)
_TJ_SUBJECT_RE = re.compile(r"\btj\b", re.I)
_MEETING_SUBJECT_RE = re.compile(r"meeting recap|invitation|call with|and joshua", re.I)
_PERSON_IN_SUBJECT_RE = re.compile(
    r"\b([A-Z][a-zA-Z'’.\-]{1,20})\s+([A-Z][a-zA-Z'’.\-]{1,30})\b"
)
_JOSH_NAME_TOKENS = frozenset({"joshua", "josh", "osborn"})

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


def _norm_addr(value: str) -> str:
    return (value or "").strip().lower()


def header_has_contact_email(headers: dict | None, email: str) -> bool:
    """True when the contact is an actual From/To participant (not Cc-only / body hit)."""
    addr = _norm_addr(email)
    if not addr or "@" not in addr:
        return False
    headers = headers or {}
    blob = f"{headers.get('from') or ''} {headers.get('to') or ''}".lower()
    return addr in blob


def subject_is_calendar_noise(subject: str) -> bool:
    low = (subject or "").strip().lower()
    return any(low.startswith(prefix) for prefix in CALENDAR_SUBJECT_PREFIXES)


def subject_is_scheduling_only(subject: str) -> bool:
    return bool(_SCHEDULING_ONLY_RE.match((subject or "").strip()))


def subject_is_tj_thread(subject: str) -> bool:
    return bool(_TJ_SUBJECT_RE.search(subject or ""))


def subject_names_other_meeting_guest(subject: str, contact_name: str = "") -> bool:
    """Skip meeting-style subjects that name someone other than this contact / Josh."""
    text = subject or ""
    if not _MEETING_SUBJECT_RE.search(text) and "your meeting recap" not in text.lower():
        return False
    own = {p.lower() for p in re.split(r"[^A-Za-z]+", contact_name or "") if len(p) > 1}
    own |= _JOSH_NAME_TOKENS
    for match in _PERSON_IN_SUBJECT_RE.finditer(text):
        first, last = match.group(1).lower(), match.group(2).lower()
        if first in _JOSH_NAME_TOKENS or last in _JOSH_NAME_TOKENS:
            continue
        if first in own or last in own:
            continue
        return True
    return False


def should_skip_nurture_thread(
    headers: dict | None,
    contact_email: str,
    contact_name: str = "",
) -> bool:
    """Calendar / TJ / other-person / client / missing-participant threads are unusable."""
    headers = {str(k).lower(): (v or "") for k, v in (headers or {}).items()}
    subject = headers.get("subject") or ""
    frm = headers.get("from") or ""
    if CALENDAR_NOTIFY_ADDR in frm.lower():
        return True
    if subject_is_calendar_noise(subject) or subject_is_scheduling_only(subject):
        return True
    if subject_is_tj_thread(subject):
        return True
    if not header_has_contact_email(headers, contact_email):
        return True
    if subject_names_other_meeting_guest(subject, contact_name):
        return True
    if is_client_context(title=subject) and not is_josh_address(contact_email):
        if any(token in subject.lower() for token in ("goliath", "peterson", "vasco", "parlay")):
            return True
    return False


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

    def find_contact_thread(self, email: str, name: str = "") -> dict[str, str] | None:
        """Most recent substantive 1:1 thread. Skips calendar, TJ, and other-person recaps."""
        addr = (email or "").strip()
        if not addr or "@" not in addr:
            return None
        query = f"(from:{addr} OR to:{addr}) (in:inbox OR in:sent) -in:chats"
        try:
            stubs = self.search(query, max_results=15)
        except Exception as exc:
            logger.warning("gmail find_contact_thread search failed: %s", exc)
            return None
        for stub in stubs or []:
            mid = str((stub or {}).get("id") or "")
            if not mid:
                continue
            try:
                msg = self.get(mid)
            except Exception as exc:
                logger.warning("gmail find_contact_thread get %s failed: %s", mid, exc)
                continue
            headers = self.headers_map(msg)
            if should_skip_nurture_thread(headers, addr, name):
                continue
            thread_id = str(msg.get("threadId") or stub.get("threadId") or "")
            if not thread_id:
                continue
            return {
                "thread_id": thread_id,
                "message_id": mid,
                "original_subject": headers.get("subject") or "",
                "in_reply_to": headers.get("message-id") or "",
                "references": headers.get("references") or headers.get("message-id") or "",
            }
        return None

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

    def send(self, to: str, subject: str, body: str) -> dict[str, Any]:
        return self.send_thread_reply(to, subject, body, thread_id="")

    def send_thread_reply(
        self,
        to: str,
        subject: str,
        body: str,
        thread_id: str,
        in_reply_to: str = "",
        references: str = "",
    ) -> dict[str, Any]:
        """Send a 1:1 reply in the original Gmail thread. Needs gmail.send."""
        from crmbrain.nurture import thread_reply_headers

        headers = thread_reply_headers(subject, in_reply_to=in_reply_to, references=references)
        msg = MIMEText(body)
        msg["to"] = to
        msg["from"] = "joshua@salesglidergrowth.com"
        msg["subject"] = headers["Subject"]
        if headers.get("In-Reply-To"):
            msg["In-Reply-To"] = headers["In-Reply-To"]
        if headers.get("References"):
            msg["References"] = headers["References"]
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        payload: dict[str, Any] = {"raw": raw}
        if thread_id:
            payload["threadId"] = thread_id
        resp = self._request(
            "POST",
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            headers={**self._headers(), "Content-Type": "application/json"},
            json=payload,
            timeout=WRITE_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json() if resp.content else {}
