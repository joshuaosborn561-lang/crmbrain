from __future__ import annotations

from crmbrain.config import Settings
from crmbrain.gmail_client import Gmail
from crmbrain.models import Engagement


def render(ev: Engagement, facts: dict | None = None, linkedin: str = "") -> str:
    facts = facts or {}
    lines = [
        f"Brief: {ev.display_name() or ev.email or ev.phone}",
        "",
        f"Company: {ev.company or 'unknown'}",
        f"Email: {ev.email or 'unknown'}",
        f"Phone: {ev.phone or 'unknown'}",
        f"LinkedIn: {linkedin or ev.linkedin_url or 'unknown'}",
        f"Role: {ev.title or 'unknown'}",
        "",
        "Why you are talking",
        ev.summary or ev.raw_subject or "No prior summary.",
        "",
        "Pain / hooks",
        facts.get("pain_points") or facts.get("relationship_hooks") or "See notes on the contact.",
        "",
        "Personal details",
        facts.get("personal_details") or facts.get("family_notes") or "None captured yet.",
        "",
        "Offer that fits",
        facts.get("gift_ideas") or "Airpods or a Loom on their exact list if they want proof first.",
    ]
    return "\n".join(lines)


def send(settings: Settings, gmail: Gmail, ev: Engagement, facts: dict | None = None) -> None:
    body = render(ev, facts)
    subject = f"Brief: {ev.display_name() or ev.company or ev.email}"
    gmail.send(settings.josh_brief_email, subject, body)
