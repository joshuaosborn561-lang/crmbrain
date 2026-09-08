from __future__ import annotations

import logging
import time
from typing import Any

import requests

from crmbrain.config import STAGE, Settings, digits_phone
from crmbrain.models import Engagement
from crmbrain import intelligence, policy

logger = logging.getLogger(__name__)

# Listing contacts for HeyReach backfill can exceed 30s; retry transient reads.
READ_TIMEOUT = 45
WRITE_TIMEOUT = 30
MAX_READ_RETRIES = 3
BACKOFF_BASE = 1.0
BACKOFF_CAP = 16.0

# HubSpot meeting engagements only. Associated emails are NOT meetings.
MEETING_ASSOCIATION_OBJECTS = ("meetings",)

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
    {
        "name": "gift_ideas",
        "label": "Gift ideas",
        "type": "string",
        "fieldType": "textarea",
        "groupName": "contactinformation",
    },
]

CONTACT_SEARCH_PROPS = [
    "email",
    "firstname",
    "lastname",
    "phone",
    "company",
    "jobtitle",
    "website",
    "hs_linkedin_url",
    "crm_source",
    "personal_details",
    "family_notes",
    "relationship_hooks",
    "pain_points",
    "buying_committee",
    "gift_ideas",
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

    def _request(
        self,
        method: str,
        path: str,
        *,
        retry: bool = False,
        timeout: int | None = None,
        **kwargs: Any,
    ) -> requests.Response:
        """HubSpot HTTP. Reads retry timeouts with backoff so one 30s stall is not fatal."""
        url = path if path.startswith("http") else f"{self.base}{path}"
        timeout = READ_TIMEOUT if timeout is None else timeout
        attempts = MAX_READ_RETRIES + 1 if retry else 1
        last_exc: BaseException | None = None
        for attempt in range(attempts):
            try:
                return self.session.request(method, url, timeout=timeout, **kwargs)
            except requests.Timeout as exc:
                last_exc = exc
                if attempt + 1 >= attempts:
                    raise
                delay = min(BACKOFF_CAP, BACKOFF_BASE * (2**attempt))
                logger.warning(
                    "hubspot %s %s timed out, retry %s/%s in %.2fs",
                    method,
                    path,
                    attempt + 1,
                    MAX_READ_RETRIES,
                    delay,
                )
                _sleep(delay)
        raise last_exc or RuntimeError("hubspot request failed")

    def ensure_properties(self) -> None:
        for prop in CONTACT_PROPS:
            resp = self._request(
                "GET", f"/crm/v3/properties/contacts/{prop['name']}", retry=True, timeout=20
            )
            if resp.status_code == 404:
                created = self._request(
                    "POST", "/crm/v3/properties/contacts", json=prop, timeout=WRITE_TIMEOUT
                )
                if created.status_code >= 400:
                    raise RuntimeError(f"create prop {prop['name']}: {created.text[:300]}")

    def _search(self, object_name: str, filters: list[dict], properties: list[str]) -> list[dict]:
        payload = {
            "filterGroups": [{"filters": filters}],
            "properties": properties,
            "limit": 10,
        }
        resp = self._request(
            "POST",
            f"/crm/v3/objects/{object_name}/search",
            json=payload,
            retry=True,
            timeout=READ_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json().get("results", [])

    def find_contact(self, email: str = "", phone: str = "", name: str = "") -> dict | None:
        if email:
            rows = self._search(
                "contacts",
                [{"propertyName": "email", "operator": "EQ", "value": email}],
                CONTACT_SEARCH_PROPS,
            )
            if rows:
                return rows[0]
        if phone:
            digits = digits_phone(phone)
            if len(digits) >= 10:
                rows = self._search(
                    "contacts",
                    [{"propertyName": "phone", "operator": "CONTAINS_TOKEN", "value": digits[-10:]}],
                    CONTACT_SEARCH_PROPS,
                )
                if rows:
                    return rows[0]
        return self._find_contact_by_name(name)

    def _find_contact_by_name(self, name: str) -> dict | None:
        """Exact first+last match. Skip if zero or multiple hits."""
        parts = [p for p in (name or "").strip().split() if p]
        if len(parts) < 2:
            return None
        first, last = parts[0], " ".join(parts[1:])
        rows = self._search(
            "contacts",
            [
                {"propertyName": "firstname", "operator": "EQ", "value": first},
                {"propertyName": "lastname", "operator": "EQ", "value": last},
            ],
            CONTACT_SEARCH_PROPS,
        )
        if len(rows) == 1:
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
            resp = self._request(
                "GET",
                "/crm/v3/objects/contacts",
                params=params,
                retry=True,
                timeout=READ_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            for row in data.get("results") or []:
                yield row
            after = (data.get("paging") or {}).get("next", {}).get("after")
            if not after:
                break

    def upsert_contact(self, ev: Engagement) -> dict:
        existing = self.find_contact(email=ev.email, phone=ev.phone, name=ev.display_name())
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
            existing_source = ((existing.get("properties") or {}).get("crm_source") or "").lower()
            if existing_source in policy.MEETING_CRM_SOURCES and ev.source not in policy.MEETING_CRM_SOURCES:
                props.pop("crm_source", None)
            resp = self._request(
                "PATCH",
                f"/crm/v3/objects/contacts/{existing['id']}",
                json={"properties": props},
                timeout=WRITE_TIMEOUT,
            )
            resp.raise_for_status()
            return resp.json()
        resp = self._request(
            "POST", "/crm/v3/objects/contacts", json={"properties": props}, timeout=WRITE_TIMEOUT
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
        resp = self._request("POST", "/crm/v3/objects/notes", json=payload, timeout=WRITE_TIMEOUT)
        if resp.status_code >= 400:
            raise RuntimeError(f"note: {resp.text[:300]}")

    def patch_contact(self, contact_id: str, properties: dict[str, Any]) -> None:
        properties = {k: v for k, v in properties.items() if v}
        if not properties:
            return
        resp = self._request(
            "PATCH",
            f"/crm/v3/objects/contacts/{contact_id}",
            json={"properties": properties},
            timeout=WRITE_TIMEOUT,
        )
        resp.raise_for_status()

    def open_deals_for_contact(self, contact_id: str) -> list[dict]:
        resp = self._request(
            "GET",
            f"/crm/v4/objects/contacts/{contact_id}/associations/deals",
            retry=True,
            timeout=READ_TIMEOUT,
        )
        if resp.status_code >= 400:
            return []
        ids = [r.get("toObjectId") or r.get("id") for r in resp.json().get("results", [])]
        deals = []
        for deal_id in ids:
            if not deal_id:
                continue
            d = self._request(
                "GET",
                f"/crm/v3/objects/deals/{deal_id}",
                params={"properties": "dealname,dealstage,pipeline,amount"},
                retry=True,
                timeout=20,
            )
            if d.ok:
                deals.append(d.json())
        return deals

    def upsert_deal(self, contact: dict, ev: Engagement, stage: str, amount: str = "") -> dict:
        contact_id = contact["id"]
        name = f"{ev.display_name() or ev.company or ev.email} — {ev.company}".strip(" —")
        existing = self.open_deals_for_contact(contact_id)
        live = [
            d
            for d in existing
            if d.get("properties", {}).get("dealstage") not in {STAGE["closed_lost"], STAGE["paid"]}
        ]
        fallback_name = ev.display_name() or ev.company or ev.email or ""
        if live:
            deal = live[0]
            current = (deal.get("properties") or {}).get("dealstage") or ""
            target = policy.choose_deal_action(current, stage, ev) if stage else None
            current_name = (deal.get("properties") or {}).get("dealname") or ""
            cleaned = policy.clean_deal_name(current_name, fallback=fallback_name)
            if target:
                self.move_deal(
                    deal["id"],
                    target,
                    evidence=f"{ev.source}:{ev.external_id}",
                    dealname=cleaned if cleaned and cleaned != current_name else "",
                )
                deal.setdefault("properties", {})["dealstage"] = target
            elif cleaned and cleaned != current_name:
                self.patch_deal(str(deal["id"]), {"dealname": cleaned})
            if cleaned and cleaned != current_name:
                deal.setdefault("properties", {})["dealname"] = cleaned
            self.fill_deal_amount(deal, amount)
            return deal
        target = policy.choose_deal_action(None, stage, ev) if stage else None
        if not target:
            return {}
        props = {
            "dealname": name or fallback_name or "SalesGlider deal",
            "dealstage": target,
            "pipeline": "default",
        }
        if amount:
            props["amount"] = amount
        payload = {
            "properties": props,
            "associations": [
                {
                    "to": {"id": contact_id},
                    "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 3}],
                }
            ],
        }
        resp = self._request("POST", "/crm/v3/objects/deals", json=payload, timeout=WRITE_TIMEOUT)
        resp.raise_for_status()
        created = resp.json()
        if amount:
            created.setdefault("properties", {})["amount"] = amount
        return created

    def fill_deal_amount(self, deal: dict, amount: str) -> bool:
        """PATCH amount only when the live deal amount is empty. Never invent."""
        hint = intelligence.amount_to_write((deal.get("properties") or {}).get("amount"), amount)
        if not hint or not deal.get("id"):
            return False
        self.patch_deal(str(deal["id"]), {"amount": hint})
        deal.setdefault("properties", {})["amount"] = hint
        return True

    def patch_deal(self, deal_id: str, properties: dict[str, Any]) -> None:
        properties = {k: v for k, v in properties.items() if v}
        if not properties:
            return
        resp = self._request(
            "PATCH",
            f"/crm/v3/objects/deals/{deal_id}",
            json={"properties": properties},
            timeout=WRITE_TIMEOUT,
        )
        resp.raise_for_status()

    def move_deal(self, deal_id: str, stage: str, evidence: str, dealname: str = "") -> None:
        props = {"dealstage": stage}
        if dealname:
            props["dealname"] = dealname
        resp = self._request(
            "PATCH",
            f"/crm/v3/objects/deals/{deal_id}",
            json={"properties": props},
            timeout=WRITE_TIMEOUT,
        )
        resp.raise_for_status()

    def upcoming_meetings(self) -> list[dict]:
        """Meetings in HubSpot engagements if available; otherwise empty (Gmail/Calendly fills this)."""
        return []

    def iter_deals(self, properties: list[str], stage: str = ""):
        after = None
        while True:
            if stage:
                payload: dict[str, Any] = {
                    "filterGroups": [
                        {"filters": [{"propertyName": "dealstage", "operator": "EQ", "value": stage}]}
                    ],
                    "properties": properties,
                    "limit": 100,
                }
                if after:
                    payload["after"] = after
                resp = self._request(
                    "POST",
                    "/crm/v3/objects/deals/search",
                    json=payload,
                    retry=True,
                    timeout=READ_TIMEOUT,
                )
            else:
                params: dict[str, Any] = {"limit": 100, "properties": ",".join(properties)}
                if after:
                    params["after"] = after
                resp = self._request(
                    "GET",
                    "/crm/v3/objects/deals",
                    params=params,
                    retry=True,
                    timeout=READ_TIMEOUT,
                )
            resp.raise_for_status()
            data = resp.json()
            for row in data.get("results") or []:
                yield row
            after = (data.get("paging") or {}).get("next", {}).get("after")
            if not after:
                break

    def contacts_for_deal(self, deal_id: str) -> list[dict]:
        resp = self._request(
            "GET",
            f"/crm/v4/objects/deals/{deal_id}/associations/contacts",
            retry=True,
            timeout=20,
        )
        if resp.status_code >= 400:
            return []
        out = []
        for row in resp.json().get("results") or []:
            cid = row.get("toObjectId") or row.get("id")
            if not cid:
                continue
            c = self._request(
                "GET",
                f"/crm/v3/objects/contacts/{cid}",
                params={
                    "properties": "email,firstname,lastname,phone,company,crm_source,hs_linkedin_url"
                },
                retry=True,
                timeout=20,
            )
            if c.ok:
                out.append(c.json())
        return out

    def contact_has_meetings(self, contact_id: str) -> bool:
        """True only for real HubSpot meeting engagements. Emails do not count."""
        for object_name in MEETING_ASSOCIATION_OBJECTS:
            resp = self._request(
                "GET",
                f"/crm/v4/objects/contacts/{contact_id}/associations/{object_name}",
                retry=True,
                timeout=20,
            )
            if resp.status_code >= 400:
                continue
            if resp.json().get("results"):
                return True
        return False

    def archive_deal(self, deal_id: str) -> None:
        resp = self._request("DELETE", f"/crm/v3/objects/deals/{deal_id}", timeout=20)
        if resp.status_code >= 400:
            # Fallback: closed-lost so junk leaves the open forecast.
            self.move_deal(deal_id, STAGE["closed_lost"], evidence="prune:archive-fallback")

    def archive_contact(self, contact_id: str) -> None:
        resp = self._request("DELETE", f"/crm/v3/objects/contacts/{contact_id}", timeout=20)
        if resp.status_code == 404:
            return
        if resp.status_code >= 400:
            raise RuntimeError(f"archive contact {contact_id}: {resp.text[:200]}")


def _sleep(seconds: float) -> None:
    if seconds > 0:
        time.sleep(seconds)
