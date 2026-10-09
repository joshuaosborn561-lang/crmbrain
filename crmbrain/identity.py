from __future__ import annotations

import re

from crmbrain.config import digits_phone

JUNK_LINKEDIN = "dnyanoba-mulgir"
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
