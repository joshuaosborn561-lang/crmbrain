"""Google OAuth refresh + tokeninfo. Never print secrets."""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass

import requests

from crmbrain.config import Settings

logger = logging.getLogger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"
TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"

GMAIL_READONLY = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_SEND = "https://www.googleapis.com/auth/gmail.send"
GMAIL_MODIFY = "https://www.googleapis.com/auth/gmail.modify"
CALENDAR_READONLY = "https://www.googleapis.com/auth/calendar.readonly"
DRIVE_READONLY = "https://www.googleapis.com/auth/drive.readonly"
DRIVE_METADATA_READONLY = "https://www.googleapis.com/auth/drive.metadata.readonly"

NEEDED_SCOPES = (
    ("calendar.readonly", CALENDAR_READONLY),
    ("drive.readonly", DRIVE_READONLY),
    ("gmail.readonly", GMAIL_READONLY),
)

_SCOPE_CACHE: dict[str, tuple[set[str], float]] = {}
_CACHE_TTL_SEC = 300.0


@dataclass(frozen=True)
class ScopeReport:
    configured: bool
    granted: tuple[str, ...]
    missing: tuple[str, ...]
    error: str = ""

    def as_text(self) -> str:
        lines = [
            "google-scopes (no secrets printed)",
            f"oauth configured: {'yes' if self.configured else 'no'}",
        ]
        if self.error:
            lines.append(f"lookup: {self.error}")
        lines.append("granted:")
        if self.granted:
            lines.extend(f"  {scope}" for scope in self.granted)
        else:
            lines.append("  (none)")
        lines.append("needed:")
        granted = set(self.granted)
        for label, scope in NEEDED_SCOPES:
            status = "OK" if _scope_granted(granted, scope) else "MISSING"
            lines.append(f"  {label:20} {status}  {scope}")
        return "\n".join(lines)


def _token_cache_key(settings: Settings) -> str:
    raw = f"{settings.gmail_client_id}|{settings.gmail_refresh_token}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def oauth_configured(settings: Settings) -> bool:
    return bool(
        settings.gmail_client_id and settings.gmail_client_secret and settings.gmail_refresh_token
    )


def refresh_access_token(settings: Settings) -> str:
    if not oauth_configured(settings):
        raise RuntimeError("Gmail OAuth client id/secret/refresh token missing")
    resp = requests.post(
        TOKEN_URL,
        data={
            "client_id": settings.gmail_client_id,
            "client_secret": settings.gmail_client_secret,
            "refresh_token": settings.gmail_refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    resp.raise_for_status()
    token = (resp.json() or {}).get("access_token") or ""
    if not token:
        raise RuntimeError("Google token endpoint returned no access_token")
    return token


def _parse_scope_string(raw: str) -> set[str]:
    return {part.strip() for part in (raw or "").split() if part.strip()}


def granted_scopes(settings: Settings, *, force: bool = False) -> set[str]:
    """Return the token's granted scopes via tokeninfo. Empty on failure."""
    if not oauth_configured(settings):
        return set()
    key = _token_cache_key(settings)
    now = time.monotonic()
    cached = _SCOPE_CACHE.get(key)
    if cached and not force and now - cached[1] < _CACHE_TTL_SEC:
        return set(cached[0])
    try:
        token = refresh_access_token(settings)
        resp = requests.get(TOKENINFO_URL, params={"access_token": token}, timeout=20)
        if resp.status_code >= 400:
            logger.warning("google tokeninfo HTTP %s", resp.status_code)
            return set()
        scopes = _parse_scope_string(str((resp.json() or {}).get("scope") or ""))
    except Exception as exc:
        logger.warning("google tokeninfo failed: %s", exc)
        return set()
    _SCOPE_CACHE[key] = (set(scopes), now)
    return scopes


def _scope_granted(scopes: set[str], needed: str) -> bool:
    if needed in scopes:
        return True
    short = needed.rsplit("/", 1)[-1]
    return any(s == short or s.endswith(f"/{short}") for s in scopes)


def has_scope(scopes: set[str], needed: str) -> bool:
    return _scope_granted(scopes, needed)


def has_drive_scope(scopes: set[str]) -> bool:
    return has_scope(scopes, DRIVE_READONLY) or has_scope(scopes, DRIVE_METADATA_READONLY)


def has_calendar_scope(scopes: set[str]) -> bool:
    return has_scope(scopes, CALENDAR_READONLY)


def has_drive_access(settings: Settings, *, probe: bool = True) -> bool:
    """True when Drive listing can run (OAuth drive.readonly or GOOGLE_API_KEY)."""
    if (settings.google_api_key or "").strip():
        return True
    if not probe:
        return False
    return has_drive_scope(granted_scopes(settings))


def drive_auth_detail(settings: Settings) -> str:
    if (settings.google_api_key or "").strip():
        return "GOOGLE_API_KEY present"
    if not oauth_configured(settings):
        return "drive.readonly scope or GOOGLE_API_KEY missing"
    if has_drive_scope(granted_scopes(settings)):
        return "oauth drive.readonly present"
    return (
        "Gmail OAuth token is missing drive.readonly; "
        "re-consent that scope or set GOOGLE_API_KEY for the public Cube folder"
    )


def report_scopes(settings: Settings) -> ScopeReport:
    configured = oauth_configured(settings)
    if not configured:
        missing = tuple(scope for _, scope in NEEDED_SCOPES)
        return ScopeReport(configured=False, granted=(), missing=missing, error="oauth not configured")
    try:
        granted = tuple(sorted(granted_scopes(settings, force=True)))
    except Exception as exc:
        granted = ()
        return ScopeReport(
            configured=True,
            granted=(),
            missing=tuple(scope for _, scope in NEEDED_SCOPES),
            error=f"lookup failed ({type(exc).__name__})",
        )
    missing = tuple(scope for _, scope in NEEDED_SCOPES if not has_scope(set(granted), scope))
    return ScopeReport(configured=True, granted=granted, missing=missing)
