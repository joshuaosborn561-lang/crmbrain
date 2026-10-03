from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import requests

from crmbrain.config import STAGE, Settings, digits_phone, is_excluded_contact, is_zoom_room_address
from crmbrain.models import Engagement
from crmbrain.names import prefer_contact_name
from crmbrain import intelligence, policy

logger = logging.getLogger(__name__)


def _amount_key(raw: object) -> str:
    try:
        return f"{float(str(raw or '').replace(',', '').strip()):.2f}"
    except ValueError:
        return ""

# Listing contacts for HeyReach backfill can exceed 30s; retry transient reads.
READ_TIMEOUT = 45
WRITE_TIMEOUT = 30
MAX_READ_RETRIES = 3
BACKOFF_BASE = 1.0
BACKOFF_CAP = 16.0
RETRYABLE_STATUS = frozenset({429, 503})

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
        # Upcoming GCal attendees may promote. Recent/past only protect from prune.
        self.scheduled_attendee_emails: set[str] = set()
        self.recent_attendee_emails: set[str] = set()
        self.dry_run = bool(getattr(settings, "dry_run", False))
        self.proposed: list[str] = []

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
        if self.dry_run and _is_hubspot_mutation(method, path):
            self.proposed.append(f"{method.upper()} {path}")
            return _DryResp()
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
                continue
            if retry and resp.status_code in RETRYABLE_STATUS and attempt + 1 < attempts:
                delay = min(BACKOFF_CAP, _retry_after_seconds(resp, _backoff_with_jitter(attempt)))
                logger.warning(
                    "hubspot %s %s HTTP %s, retry %s/%s in %.2fs",
                    method,
                    path,
                    resp.status_code,
                    attempt + 1,
                    MAX_READ_RETRIES,
                    delay,
                )
                _sleep(delay)
                continue
            return resp
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
        if email and is_zoom_room_address(email):
            return None
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

    def _is_excluded(self, ev: Engagement | None = None, contact: dict | None = None) -> bool:
        return is_excluded_contact(ev, contact)

    def upsert_contact(self, ev: Engagement) -> dict:
        if self._is_excluded(ev):
            logger.info("skip hubspot contact write for excluded person")
            return {"id": "", "properties": {}, "skipped": "non_deal"}
        existing = self.find_contact(email=ev.email, phone=ev.phone, name=ev.display_name())
        existing_props = (existing or {}).get("properties") or {}
        first = prefer_contact_name(
            existing_props.get("firstname") or "",
            ev.first_name
            or (ev.display_name().split(" ")[0] if ev.display_name() else ""),
        )
        last = prefer_contact_name(
            existing_props.get("lastname") or "",
            ev.last_name
            or (" ".join(ev.display_name().split(" ")[1:]) if ev.display_name() else ""),
        )
        props = {
            "firstname": first,
            "lastname": last,
            "company": ev.company or existing_props.get("company") or "",
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
            existing_source = (existing_props.get("crm_source") or "").lower()
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

    def add_note(
        self,
        contact_id: str,
        body: str,
        ev: Engagement | None = None,
        contact: dict | None = None,
    ) -> None:
        if not contact_id or self._is_excluded(ev, contact):
            if self._is_excluded(ev, contact):
                logger.info("skip hubspot note for excluded person")
            return
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

    def patch_contact(
        self,
        contact_id: str,
        properties: dict[str, Any],
        ev: Engagement | None = None,
        contact: dict | None = None,
    ) -> None:
        if not contact_id or self._is_excluded(ev, contact):
            if self._is_excluded(ev, contact):
                logger.info("skip hubspot contact patch for excluded person")
            return
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
                params={
                    "properties": (
                        "dealname,dealstage,pipeline,amount,dealtype,"
                        "hs_mrr,hs_arr,hs_acv,hs_tcv,hs_is_closed_won"
                    )
                },
                retry=True,
                timeout=20,
            )
            if d.ok:
                deals.append(d.json())
        return deals

    def _archive_duplicate_deals(self, deals: list[dict]) -> list[dict]:
        """Soft-archive same-stage, no-amount duplicates. Keep the richer deal."""
        archived_ids: set[str] = set()
        for _keep, dup in policy.duplicate_open_deal_pairs(deals):
            dup_id = str(dup.get("id") or "")
            if not dup_id or dup_id in archived_ids:
                continue
            try:
                self.archive_deal(dup_id)
                archived_ids.add(dup_id)
            except Exception as exc:
                logger.warning("dedupe archive %s failed: %s", dup_id, exc)
        if not archived_ids:
            return deals
        return [d for d in deals if str(d.get("id") or "") not in archived_ids]

    def search_contacts_by_email_token(self, token: str) -> list[dict]:
        if not token:
            return []
        return self._search(
            "contacts",
            [{"propertyName": "email", "operator": "CONTAINS_TOKEN", "value": token}],
            ["email", "firstname", "lastname", "phone", "company", "crm_source"],
        )

    def find_contact_by_company(self, company: str) -> dict | None:
        raw = (company or "").strip()
        if len(raw) < 3:
            return None
        rows = self._search(
            "contacts",
            [{"propertyName": "company", "operator": "CONTAINS_TOKEN", "value": raw}],
            CONTACT_SEARCH_PROPS,
        )
        if len(rows) == 1:
            return rows[0]
        return None

    def find_deal_by_amount(self, amount: str) -> dict | None:
        key = _amount_key(amount)
        if not key:
            return None
        rows = self._search(
            "deals",
            [{"propertyName": "amount", "operator": "EQ", "value": amount.strip()}],
            ["dealname", "dealstage", "amount"],
        )
        hits = [row for row in rows if _amount_key((row.get("properties") or {}).get("amount")) == key]
        if len(hits) == 1:
            return hits[0]
        if not hits:
            # HubSpot may store 2875.5 vs 2875.50 — scan open deals when EQ misses.
            try:
                for deal in self.iter_deals(["dealname", "dealstage", "amount"]):
                    if _amount_key((deal.get("properties") or {}).get("amount")) == key:
                        hits.append(deal)
            except Exception:
                hits = []
            if len(hits) == 1:
                return hits[0]
        return None

    def find_contact_for_commerce(
        self, name: str = "", company: str = "", amount: str = "", email: str = ""
    ) -> dict | None:
        """Match a payment or agreement mail to one CRM contact."""
        if email:
            found = self.find_contact(email=email)
            if found:
                return found
            token = email.split("@", 1)[0]
            if token and hasattr(self, "search_contacts_by_email_token"):
                rows = self.search_contacts_by_email_token(email) or self.search_contacts_by_email_token(token)
                if len(rows) == 1:
                    return rows[0]
        if name:
            found = self._find_contact_by_name(name)
            if found:
                return found
        if company:
            found = self.find_contact_by_company(company)
            if found:
                return found
        if amount:
            deal = self.find_deal_by_amount(amount)
            if deal and hasattr(self, "contacts_for_deal"):
                contacts = self.contacts_for_deal(str(deal.get("id") or ""))
                if len(contacts) == 1:
                    return contacts[0]
        return None

    def _apply_live_deal(self, deal: dict, ev: Engagement, stage: str, amount: str, contact: dict) -> dict:
        current = (deal.get("properties") or {}).get("dealstage") or ""
        target = policy.choose_deal_action(current, stage, ev, deal=deal) if stage else None
        current_name = (deal.get("properties") or {}).get("dealname") or ""
        wanted = policy.deal_name_for(ev, contact)
        cleaned = policy.prefer_deal_name(
            policy.clean_deal_name(current_name, fallback=wanted),
            wanted,
        )
        if policy.is_weak_deal_name(current_name) and wanted:
            cleaned = wanted
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
        self.fill_deal_amount(deal, amount, ev=ev, contact=contact)
        return deal

    def upsert_deal(self, contact: dict, ev: Engagement, stage: str, amount: str = "") -> dict:
        if self._is_excluded(ev, contact):
            logger.info("skip hubspot deal write for excluded person")
            return {}
        contact_id = contact["id"]
        existing = self._archive_duplicate_deals(self.open_deals_for_contact(contact_id))
        live = policy.live_open_deals(existing)
        if live:
            deal = max(live, key=policy.deal_richness)
            return self._apply_live_deal(deal, ev, stage, amount, contact)
        won = policy.closed_won_deals(existing)
        if won and stage == STAGE["paid"]:
            deal = max(won, key=policy.deal_richness)
            return self._apply_live_deal(deal, ev, stage, amount, contact)
        if policy.blocks_no_show_create(existing, stage):
            return {}
        target = policy.choose_deal_action(None, stage, ev) if stage else None
        if not target:
            return {}
        # HubSpot workflows can create a deal between the first read and POST.
        existing = self._archive_duplicate_deals(self.open_deals_for_contact(contact_id))
        live = policy.live_open_deals(existing)
        if live:
            deal = max(live, key=policy.deal_richness)
            return self._apply_live_deal(deal, ev, stage, amount, contact)
        won = policy.closed_won_deals(existing)
        if won and stage == STAGE["paid"]:
            deal = max(won, key=policy.deal_richness)
            return self._apply_live_deal(deal, ev, stage, amount, contact)
        if policy.blocks_no_show_create(existing, stage):
            return {}
        name = policy.deal_name_for(ev, contact) or ev.email or "SalesGlider deal"
        props = {
            "dealname": name,
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
            extra = ev.extra or {}
            note = intelligence.amount_citation_note(
                ev, amount, extra.get("deal_terms") if isinstance(extra.get("deal_terms"), dict) else None
            )
            try:
                self.add_note(str(contact_id), note, ev=ev, contact=contact)
            except Exception as exc:
                logger.warning("amount note failed %s: %s", contact_id, exc)
        return created

    def fill_deal_amount(
        self,
        deal: dict,
        amount: str,
        ev: Engagement | None = None,
        contact: dict | None = None,
    ) -> bool:
        """PATCH amount by source priority. Paid only from doc/payment evidence."""
        if self._is_excluded(ev, contact):
            logger.info("skip hubspot amount write for excluded person")
            return False
        hint = intelligence.deal_amount_to_write(deal, amount, ev=ev)
        if not hint or not deal.get("id"):
            return False
        if intelligence.amounts_equal((deal.get("properties") or {}).get("amount"), hint):
            return False
        self.patch_deal(str(deal["id"]), {"amount": hint})
        deal.setdefault("properties", {})["amount"] = hint
        if contact and contact.get("id"):
            extra = (ev.extra if ev else {}) or {}
            note = intelligence.amount_citation_note(
                ev, hint, extra.get("deal_terms") if isinstance(extra.get("deal_terms"), dict) else None
            )
            try:
                self.add_note(str(contact["id"]), note, ev=ev, contact=contact)
            except Exception as exc:
                logger.warning("amount note failed %s: %s", contact.get("id"), exc)
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
        return bool(self._meeting_association_ids(contact_id))

    def _meeting_association_ids(self, contact_id: str) -> list[str]:
        ids: list[str] = []
        for object_name in MEETING_ASSOCIATION_OBJECTS:
            resp = self._request(
                "GET",
                f"/crm/v4/objects/contacts/{contact_id}/associations/{object_name}",
                retry=True,
                timeout=20,
            )
            if resp.status_code >= 400:
                continue
            for row in resp.json().get("results") or []:
                mid = row.get("toObjectId") or row.get("id")
                if mid:
                    ids.append(str(mid))
        return ids

    def contact_has_future_meetings(self, contact_id: str, now=None) -> bool:
        """True when a HubSpot meeting engagement is still in the future."""
        from crmbrain.config import now_utc
        from datetime import datetime, timezone

        now = now or now_utc()
        ids = self._meeting_association_ids(contact_id)
        if not ids:
            return False
        unknown = False
        for mid in ids:
            resp = self._request(
                "GET",
                f"/crm/v3/objects/meetings/{mid}",
                params={"properties": "hs_meeting_start_time,hs_timestamp"},
                retry=True,
                timeout=20,
            )
            if resp.status_code >= 400:
                unknown = True
                continue
            props = (resp.json() or {}).get("properties") or {}
            raw = props.get("hs_meeting_start_time") or props.get("hs_timestamp")
            if not raw:
                unknown = True
                continue
            try:
                if str(raw).isdigit():
                    stamp = datetime.fromtimestamp(int(raw) / 1000, tz=timezone.utc)
                else:
                    stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                    if stamp.tzinfo is None:
                        stamp = stamp.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError, OSError):
                unknown = True
                continue
            if stamp > now:
                return True
        return unknown

    def count_open_deals(self) -> int:
        n = 0
        try:
            for deal in self.iter_deals(["dealstage"]):
                stage = (deal.get("properties") or {}).get("dealstage") or ""
                if stage and stage != STAGE["closed_lost"]:
                    n += 1
        except Exception:
            return n
        return n

    def disassociate_contact_from_deal(self, contact_id: str, deal_id: str) -> None:
        resp = self._request(
            "DELETE",
            f"/crm/v4/objects/contacts/{contact_id}/associations/deals/{deal_id}",
            timeout=WRITE_TIMEOUT,
        )
        if resp.status_code >= 400 and resp.status_code != 404:
            raise RuntimeError(f"detach contact {contact_id} from deal {deal_id}: {resp.text[:200]}")

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


def _backoff_with_jitter(attempt: int) -> float:
    base = min(BACKOFF_CAP, BACKOFF_BASE * (2**attempt))
    return min(BACKOFF_CAP, base * (0.5 + random.random()))


def _is_hubspot_mutation(method: str, path: str) -> bool:
    """True for real writes. HubSpot /search POSTs are reads."""
    verb = (method or "").upper()
    if verb in {"PATCH", "PUT", "DELETE"}:
        return True
    if verb != "POST":
        return False
    return "/search" not in (path or "")


class _DryResp:
    status_code = 200
    text = "{}"
    content = b"{}"
    ok = True

    def json(self) -> dict:
        return {"id": "dry-run", "properties": {}, "results": []}

    def raise_for_status(self) -> None:
        return None
