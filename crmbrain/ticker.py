from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from crmbrain.config import now_utc
from crmbrain.memory import Memory
from crmbrain.models import Engagement


def enroll(memory: Memory, ev: Engagement, reason: str, hs_contact_id: str = "", hs_deal_id: str = "") -> dict:
    next_fire = (now_utc() + timedelta(days=90)).isoformat()
    row = {
        "id": str(uuid4()),
        "name": ev.display_name(),
        "email": ev.email or None,
        "phone": ev.phone or None,
        "company": ev.company or None,
        "hs_contact_id": hs_contact_id or None,
        "hs_deal_id": hs_deal_id or None,
        "reason": reason,
        "status": "active",
        "next_fire_at": next_fire,
    }
    memory.enroll_ticker(row)
    return row


def draft_email(name: str, company: str, reason: str) -> tuple[str, str]:
    first = (name or "there").split(" ")[0]
    subject = {
        "no_show": "Still around?",
        "never_booked": "Quick bump",
        "kicked_can": "Timing better?",
        "deal_died": "Worth a look?",
    }.get(reason, "Quick bump")
    body = (
        f"Hey {first},\n\n"
        f"Wanted to bump this in case timing is better on {company or 'your side'}.\n\n"
        "We are still booking qualified meetings for owners... $2M pipeline and $100K closed from the same motion.\n\n"
        "I can send a Loom or throw Airpods at a test list if that makes it easy.\n\n"
        "worth sharing more?"
    )
    if "—" in body or "–" in body or "—" in subject:
        body = body.replace("—", "...").replace("–", "...")
    return subject, body
