from __future__ import annotations

import re
from typing import Any

import requests

from crmbrain.config import Settings, digits_phone

BASE = "https://api.leadmagic.io/v1"
JUNK_LINKEDIN = "dnyanoba-mulgir"
SKIP_EMAIL_DOMAINS = {
    "salesglidergrowth.com",
    "salescloudedgroup.com",
    "gmail.com",
    "yahoo.com",
    "hotmail.com",
    "outlook.com",
    "icloud.com",
    "aol.com",
}
SKIP_EMAILS = {
    "booking-bridge-sync-test@salesglidergrowth.com",
    "fred@fireflies.ai",
    "joshua@jmosolutionsllc.com",
}


def usable_linkedin(url: str | None) -> str:
    raw = (url or "").strip().split("?")[0].rstrip("/")
    if not raw or JUNK_LINKEDIN in raw.lower():
        return ""
    if re.fullmatch(r"[A-Za-z0-9_-]{3,}", raw) and "linkedin.com" not in raw.lower():
        return f"https://www.linkedin.com/in/{raw}"
    if raw.lower().startswith("in/"):
        return f"https://www.linkedin.com/{raw}"
    if "linkedin.com/in/" not in raw.lower():
        return ""
    if raw.startswith("http"):
        return raw
    return "https://" + raw.lstrip("/")


def domain_of(email: str) -> str:
    if email and "@" in email:
        return email.split("@", 1)[1].lower()
    return ""


def should_skip_email(email: str) -> bool:
    email = (email or "").strip().lower()
    if not email:
        return True
    if email in SKIP_EMAILS:
        return True
    return domain_of(email) in {"salesglidergrowth.com", "salescloudedgroup.com"}


def normalize_phone(value: str | None) -> str:
    digits = digits_phone(value)
    if len(digits) < 10:
        return ""
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return f"+{digits}" if not (value or "").startswith("+") else value or f"+{digits}"


def _headers(settings: Settings) -> dict[str, str]:
    return {"X-API-Key": settings.leadmagic_key, "Content-Type": "application/json"}


def parse_email_response(data: dict[str, Any] | None) -> str:
    if not isinstance(data, dict):
        return ""
    status = str(data.get("status") or "").lower()
    email = (
        data.get("email")
        or (data.get("data") or {}).get("email")
        or data.get("work_email")
        or ""
    )
    email = str(email).strip().lower()
    if not email or "@" not in email:
        return ""
    if status and status not in {"valid", "found", "success", "ok"}:
        # Some payloads omit status and still return an email.
        if status in {"invalid", "not_found", "error"}:
            return ""
    return email


def parse_mobile_response(data: dict[str, Any] | None) -> str:
    if not isinstance(data, dict):
        return ""
    mobile = data.get("mobile_number") or data.get("mobile") or data.get("phone") or ""
    return normalize_phone(str(mobile) if mobile else "")


def find_email(settings: Settings, first: str, last: str, domain: str = "", company: str = "") -> str:
    if not settings.leadmagic_key:
        return ""
    if not (first or last) or not (domain or company):
        return ""
    payload: dict[str, str] = {}
    if first:
        payload["first_name"] = first
    if last:
        payload["last_name"] = last
    if domain and domain.lower() not in SKIP_EMAIL_DOMAINS:
        payload["domain"] = domain
    if company:
        payload["company_name"] = company
    if "domain" not in payload and "company_name" not in payload:
        return ""
    try:
        resp = requests.post(
            f"{BASE}/people/email-finder",
            headers=_headers(settings),
            json=payload,
            timeout=40,
        )
        if resp.status_code >= 400:
            return ""
        return parse_email_response(resp.json() if resp.text else {})
    except Exception:
        return ""


def find_mobile(settings: Settings, work_email: str = "", linkedin_url: str = "") -> str:
    if not settings.leadmagic_key:
        return ""
    if should_skip_email(work_email) and not usable_linkedin(linkedin_url):
        return ""
    payload: dict[str, str] = {}
    if work_email and "@" in work_email and not should_skip_email(work_email):
        if domain_of(work_email) in SKIP_EMAIL_DOMAINS:
            payload["personal_email"] = work_email
        else:
            payload["work_email"] = work_email
    li = usable_linkedin(linkedin_url)
    if li:
        payload["profile_url"] = li
    if not payload:
        return ""
    try:
        resp = requests.post(
            f"{BASE}/people/mobile-finder",
            headers=_headers(settings),
            json=payload,
            timeout=40,
        )
        if resp.status_code >= 400:
            return ""
        return parse_mobile_response(resp.json() if resp.text else {})
    except Exception:
        return ""


def parse_profile_response(data: dict[str, Any] | None) -> str:
    if not isinstance(data, dict):
        return ""
    url = data.get("profile_url") or data.get("linkedin_url") or data.get("linkedin") or ""
    return usable_linkedin(str(url) if url else "")


def find_profile(settings: Settings, email: str) -> str:
    """Email → LinkedIn URL. LeadMagic charges only when found."""
    if not settings.leadmagic_key or not looks_like_email(email) or should_skip_email(email):
        return ""
    payload = (
        {"personal_email": email}
        if domain_of(email) in SKIP_EMAIL_DOMAINS
        else {"work_email": email}
    )
    try:
        resp = requests.post(
            f"{BASE}/people/b2b-profile",
            headers=_headers(settings),
            json=payload,
            timeout=40,
        )
        if resp.status_code >= 400:
            return ""
        return parse_profile_response(resp.json() if resp.text else {})
    except Exception:
        return ""


def looks_like_phone(value: str | None) -> bool:
    if not value:
        return False
    if "*" in value:
        return True  # HubSpot masked phone already exists
    return len(digits_phone(value)) >= 10


def looks_like_email(value: str | None) -> bool:
    if not value:
        return False
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", value.strip()))
