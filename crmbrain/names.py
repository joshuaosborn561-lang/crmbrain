"""Person-name helpers. Meeting titles are never a person's name."""

from __future__ import annotations

import re

# Titles / calendar leftovers that must never land in firstname/lastname.
_TITLE_MARKERS = (
    "(google calendar)",
    "google calendar",
    "zoom meeting",
    "ms teams",
    "microsoft teams",
    "discovery call",
    "disco call",
    "intro call",
    "salesglider intro",
    "pipeline review",
    "sync:",
)
_TITLE_SEPARATORS = (" — ", " – ", " | ")
_CONFIDENT_LOCAL = re.compile(r"^[A-Za-z]{2,}[._-][A-Za-z]{2,}(?:[._-][A-Za-z]{2,})?$")
_PERSON_TOKEN = re.compile(r"^[A-Za-z][A-Za-z'\-]{1,30}$")


def looks_like_meeting_title(name: str) -> bool:
    """True when `name` is a calendar/Fireflies title, not a person."""
    text = (name or "").strip()
    if not text:
        return False
    low = text.lower()
    if any(marker in low for marker in _TITLE_MARKERS):
        return True
    if any(sep in text for sep in _TITLE_SEPARATORS) and len(text.split()) >= 4:
        return True
    if " and " in low and len(text.split()) >= 4:
        return True
    if len(text) > 48:
        return True
    return False


def is_confident_person_name(name: str) -> bool:
    """Two-or-three token display name that does not look like a meeting title."""
    text = (name or "").strip()
    if not text or looks_like_meeting_title(text):
        return False
    parts = [p for p in re.split(r"\s+", text) if p]
    if len(parts) < 2 or len(parts) > 4:
        return False
    return all(_PERSON_TOKEN.match(p.rstrip(".,")) for p in parts)


def split_person_name(name: str) -> tuple[str, str]:
    parts = [p for p in re.split(r"\s+", (name or "").strip()) if p]
    if not parts:
        return "", ""
    return parts[0], " ".join(parts[1:])


def name_from_email_local(email: str) -> tuple[str, str]:
    """Safe guess from the local part only when it is first.last / first_last.

    `bdonigan` is not confident — leave blank rather than invent a first name.
    """
    local = (email or "").strip().split("@")[0]
    if not local or not _CONFIDENT_LOCAL.match(local):
        return "", ""
    bits = [b for b in re.split(r"[._-]+", local) if b.isalpha() and len(b) >= 2]
    if len(bits) < 2 or len(bits) > 3:
        return "", ""
    titled = [b[:1].upper() + b[1:].lower() for b in bits]
    return titled[0], " ".join(titled[1:])


def person_name_from_attendee(display_name: str, email: str = "") -> tuple[str, str]:
    """Prefer calendar/Fireflies attendee display name; else a confident email guess."""
    if is_confident_person_name(display_name):
        return split_person_name(display_name)
    if display_name and not looks_like_meeting_title(display_name):
        first, last = split_person_name(display_name)
        if first and _PERSON_TOKEN.match(first.rstrip(".,")):
            if last and not is_confident_person_name(f"{first} {last}"):
                last = ""
            return first, last
    return name_from_email_local(email)


def prefer_contact_name(existing: str, candidate: str) -> str:
    """Keep a good HubSpot name. Never replace it with a meeting title."""
    existing = (existing or "").strip()
    candidate = (candidate or "").strip()
    if looks_like_meeting_title(candidate):
        return existing
    if not candidate:
        return existing
    if not existing or looks_like_meeting_title(existing):
        return candidate
    return existing


def format_deal_name(first: str = "", last: str = "", company: str = "", fallback: str = "") -> str:
    """`First Last - Company`. Never a meeting title."""
    person = f"{(first or '').strip()} {(last or '').strip()}".strip()
    if person and looks_like_meeting_title(person):
        person = ""
    if fallback and not person and not looks_like_meeting_title(fallback):
        person = fallback.strip()
    company = (company or "").strip()
    if company and looks_like_meeting_title(company):
        company = ""
    if person and company:
        if person.lower() == company.lower():
            return person
        return f"{person} - {company}"
    return person or company


def is_weak_deal_name(name: str) -> bool:
    text = (name or "").strip()
    if not text:
        return True
    if looks_like_meeting_title(text):
        return True
    if re.fullmatch(r"[\s\-–—]+", text):
        return True
    if re.search(r"[\s]*[-–—][\s]*$", text):
        return True
    return False


def prefer_deal_name(current: str, wanted: str) -> str:
    """Upgrade a stub/title deal name; keep a richer existing one."""
    current = (current or "").strip()
    wanted = (wanted or "").strip()
    if not wanted:
        return current
    if is_weak_deal_name(current) or looks_like_meeting_title(current):
        return wanted
    if is_weak_deal_name(wanted):
        return current
    stripped = re.sub(r"[\s]*[-–—][\s]*$", "", current)
    if wanted.startswith(stripped) and len(wanted) > len(stripped):
        return wanted
    return current


def parse_attendee_token(raw: str) -> tuple[str, str]:
    """Return (display_name, email) from `Name <email>` or a bare email/name."""
    text = (raw or "").strip().strip('"').strip("'")
    if not text:
        return "", ""
    match = re.match(r"(.+?)\s*<([^>]+@[^>]+)>", text)
    if match:
        return match.group(1).strip().strip('"'), match.group(2).strip().lower()
    if "@" in text and " " not in text:
        return "", text.lower()
    return text, ""


def _levenshtein(left: str, right: str) -> int:
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)
    prev = list(range(len(right) + 1))
    for i, ca in enumerate(left, 1):
        curr = [i]
        for j, cb in enumerate(right, 1):
            insert = curr[j - 1] + 1
            delete = prev[j] + 1
            replace = prev[j - 1] + (0 if ca == cb else 1)
            curr.append(min(insert, delete, replace))
        prev = curr
    return prev[-1]


def _mac_mc_key(last: str) -> str:
    """Treat MacAntosh / McAntosh as the same last-name key."""
    low = (last or "").strip().lower()
    if low.startswith("mac") and len(low) > 3:
        return "mc" + low[3:]
    return low


def names_fuzzy_match(left: str, right: str) -> bool:
    """True when two display names are the same person (prefix / Mac-Mc / typo)."""
    a = " ".join((left or "").lower().split())
    b = " ".join((right or "").lower().split())
    if not a or not b:
        return False
    if a == b:
        return True
    first_a, last_a = split_person_name(a)
    first_b, last_b = split_person_name(b)
    if not first_a or not last_a or not first_b or not last_b:
        return False
    first_ok = first_a == first_b or first_a.startswith(first_b) or first_b.startswith(first_a)
    if not first_ok or min(len(first_a), len(first_b)) < 2:
        return False
    if last_a == last_b or last_a in last_b or last_b in last_a:
        return True
    mac_a, mac_b = _mac_mc_key(last_a), _mac_mc_key(last_b)
    if mac_a == mac_b and len(mac_a) >= 4:
        return True
    if min(len(last_a), len(last_b)) >= 5 and _levenshtein(last_a, last_b) <= 2:
        return True
    return False
