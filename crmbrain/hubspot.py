from __future__ import annotations

from typing import Any

import requests

from crmbrain.config import STAGE, Settings, digits_phone
from crmbrain.models import Engagement

CONTACT_PROPS = [
    {
        "name": "personal_details",
        "label": "Personal details",
        "type": "string",
        "fieldType": "textarea",
        "groupName": "contactinformation",
        "description": "Relationship facts: family, hobbies, school, what matters to them.",
    },
    {
        "name": "family_notes",
        "label": "Family notes",
        "type": "string",
        "fieldType": "textarea",
        "groupName": "contactinformation",
    },
    {
        "name": "relationship_hooks",
        "label": "Relationship hooks",
        "type": "string",
        "fieldType": "textarea",
        "groupName": "contactinformation",
    },
    {
        "name": "pain_points",
        "label": "Pain points",
        "type": "string",
        "fieldType": "textarea",
        "groupName": "contactinformation",
    },
    {
        "name": "buying_committee",
        "label": "Buying committee",
        "type": "string",
        "fieldType": "textarea",
        "groupName": "contactinformation",
    },
    {
        "name": "crm_source",
        "label": "CRM source",
        "type": "string",
        "fieldType": "text",
        "groupName": "contactinformation",
        "description": "How this person earned a HubSpot record: call, meeting, reply, LinkedIn, Allo, RVM.",
    },
]


class HubSpot:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.base = "https://api.hubapi.com"
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {settings.hubspot_token}",
                "Content-Type": "application/json",
            }
        )

    def ensure_properties(self) -> None:
        for prop in CONTACT_PROPS:
            resp = self.session.get(f"{self.base}/crm/v3/properties/contacts/{prop['name']}", timeout=20)
            if resp.status_code == 404:
                created = self.session.post(
                    f"{self.base}/crm/v3/properties/contacts", json=prop, timeout=20
                )
                if created.status_code >= 400:
                    raise RuntimeError(f"create prop {prop['name']}: {created.text[:300]}")

    def _search(self, object_name: str, filters: list[dict], properties: list[str]) -> list[dict]:
        payload = {
            "filterGroups": [{"filters": filters}],
            "properties": properties,
            "limit": 10,
        }
        resp = self.session.post(
            f"{self.base}/crm/v3/objects/{object_name}/search", json=payload, timeout=30
        )
        resp.raise_for_status()
        return resp.json().get("results", [])

    def find_contact(self, email: str = "", phone: str = "", name: str = "") -> dict | None:
        if email:
            rows = self._search(
                "contacts",
                [{"propertyName": "email", "operator": "EQ", "value": email}],
                [
                    "email",
                    "firstname",
                    "lastname",
                    "phone",
                    "company",
                    "jobtitle",
                    "website",
                    "hs_linkedin_url",
                    "personal_details",
                    "family_notes",
                    "relationship_hooks",
                    "pain_points",
                    "gift_ideas",
                ],
            )
            if rows:
                return rows[0]
        if phone:
            digits = digits_phone(phone)
            if len(digits) >= 10:
                rows = self._search(
                    "contacts",
                    [{"propertyName": "phone", "operator": "CONTAINS_TOKEN", "value": digits[-10:]}],
                    ["email", "firstname", "lastname", "phone", "company"],
                )
                if rows:
                    return rows[0]
        return None

    def in_crm(self, email: str = "", phone: str = "") -> bool:
        return self.find_contact(email=email, phone=phone) is not None

    def iter_contacts(self, properties: list[str]):
        after = None
        while True:
            params: dict[str, Any] = {"limit": 100, "properties": ",".join(properties)}
            if after:
                params["after"] = after
            resp = self.session.get(f"{self.base}/crm/v3/objects/contacts", params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            for row in data.get("results") or []:
                yield row
            after = (data.get("paging") or {}).get("next", {}).get("after")
            if not after:
                break

    def upsert_contact(self, ev: Engagement) -> dict:
        existing = self.find_contact(email=ev.email, phone=ev.phone)
        props = {
            "firstname": ev.first_name or (ev.display_name().split(" ")[0] if ev.display_name() else ""),
            "lastname": ev.last_name
            or (" ".join(ev.display_name().split(" ")[1:]) if ev.display_name() else ""),
            "company": ev.company,
            "jobtitle": ev.title,
            "crm_source": ev.source,
        }
        if ev.email:
            props["email"] = ev.email
        if ev.phone:
            props["phone"] = ev.phone
        if ev.linkedin_url:
            props["hs_linkedin_url"] = ev.linkedin_url
        props = {k: v for k, v in props.items() if v}
        if existing:
            resp = self.session.patch(
                f"{self.base}/crm/v3/objects/contacts/{existing['id']}",
                json={"properties": props},
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json()
        resp = self.session.post(
            f"{self.base}/crm/v3/objects/contacts", json={"properties": props}, timeout=30
        )
        resp.raise_for_status()
        return resp.json()

    def add_note(self, contact_id: str, body: str) -> None:
        payload = {
            "properties": {"hs_timestamp": str(int(__import__("time").time() * 1000)), "hs_note_body": body},
            "associations": [
                {
                    "to": {"id": contact_id},
                    "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}],
                }
            ],
        }
        resp = self.session.post(f"{self.base}/crm/v3/objects/notes", json=payload, timeout=30)
        if resp.status_code >= 400:
            raise RuntimeError(f"note: {resp.text[:300]}")

    def patch_contact(self, contact_id: str, properties: dict[str, Any]) -> None:
        properties = {k: v for k, v in properties.items() if v}
        if not properties:
            return
        resp = self.session.patch(
            f"{self.base}/crm/v3/objects/contacts/{contact_id}",
            json={"properties": properties},
            timeout=30,
        )
        resp.raise_for_status()

    def open_deals_for_contact(self, contact_id: str) -> list[dict]:
        resp = self.session.get(
            f"{self.base}/crm/v4/objects/contacts/{contact_id}/associations/deals",
            timeout=30,
        )
        if resp.status_code >= 400:
            return []
        ids = [r.get("toObjectId") or r.get("id") for r in resp.json().get("results", [])]
        deals = []
        for deal_id in ids:
            if not deal_id:
                continue
            d = self.session.get(
                f"{self.base}/crm/v3/objects/deals/{deal_id}",
                params={"properties": "dealname,dealstage,pipeline,amount"},
                timeout=20,
            )
            if d.ok:
                deals.append(d.json())
        return deals

    def upsert_deal(self, contact: dict, ev: Engagement, stage: str) -> dict:
        contact_id = contact["id"]
        name = f"{ev.display_name() or ev.company or ev.email} — {ev.company}".strip(" —")
        existing = self.open_deals_for_contact(contact_id)
        live = [
            d
            for d in existing
            if d.get("properties", {}).get("dealstage") not in {STAGE["closed_lost"], STAGE["paid"]}
        ]
        if live:
            deal = live[0]
            self.move_deal(deal["id"], stage, evidence=f"{ev.source}:{ev.external_id}")
            return deal
        payload = {
            "properties": {
                "dealname": name or "SalesGlider deal",
                "dealstage": stage,
                "pipeline": "default",
            },
            "associations": [
                {
                    "to": {"id": contact_id},
                    "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 3}],
                }
            ],
        }
        resp = self.session.post(f"{self.base}/crm/v3/objects/deals", json=payload, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def move_deal(self, deal_id: str, stage: str, evidence: str) -> None:
        resp = self.session.patch(
            f"{self.base}/crm/v3/objects/deals/{deal_id}",
            json={"properties": {"dealstage": stage}},
            timeout=30,
        )
        resp.raise_for_status()

    def upcoming_meetings(self) -> list[dict]:
        """Meetings in HubSpot engagements if available; otherwise empty (Gmail/Calendly fills this)."""
        return []
