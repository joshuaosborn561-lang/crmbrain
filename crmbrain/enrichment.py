from __future__ import annotations

from crmbrain.config import Settings
from crmbrain.http_mcp import McpClient
from crmbrain.models import Engagement


def enrich(settings: Settings, ev: Engagement) -> Engagement:
    if ev.linkedin_url and ev.email:
        return ev
    if not settings.enrichment_url:
        return ev
    try:
        client = McpClient(settings.enrichment_url)
        client.initialize()
        lead = {
            "first_name": ev.first_name,
            "last_name": ev.last_name,
            "full_name": ev.display_name(),
            "company": ev.company,
            "domain": ev.domain,
            "email": ev.email,
            "linkedin_url": ev.linkedin_url,
        }
        result = client.call(
            "enrich_single",
            {"lead": lead, "client_tag": settings.enrichment_client_tag},
        )
        if not isinstance(result, dict):
            result = client.call(
                "enrich_waterfall",
                {
                    "client_tag": settings.enrichment_client_tag,
                    "rows": [lead],
                    "need": "person",
                },
            )
        person = result if isinstance(result, dict) else {}
        contacts = person.get("contacts") or person.get("people") or [person]
        row = contacts[0] if contacts and isinstance(contacts[0], dict) else person
        ev.linkedin_url = ev.linkedin_url or row.get("linkedin_url") or row.get("linkedin") or ""
        ev.email = ev.email or row.get("email") or row.get("work_email") or ""
        ev.phone = ev.phone or row.get("phone") or ""
        ev.company = ev.company or row.get("company") or ""
    except Exception:
        return ev
    return ev
