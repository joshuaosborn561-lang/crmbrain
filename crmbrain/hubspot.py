from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import requests

from crmbrain.config import (
    LOST_REASONS,
    NO_SHOW_HINT,
    SG_DEAL_TYPES,
    STAGE,
    Settings,
    digits_phone,
    is_deleted_stage,
    is_excluded_contact,
    is_zoom_room_address,
)
from crmbrain.models import Engagement
from crmbrain.names import names_fuzzy_match, prefer_contact_name
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
        "name": "nurture_thread_id",
        "label": "Nurture thread id",
        "type": "string",
        "fieldType": "text",
        "groupName": "contactinformation",
        "description": "Gmail thread started by the first #nurture email. Later nurture touches reply here only.",
    },
    {
        "name": "nurture_thread_subject",
        "label": "Nurture thread subject",
        "type": "string",
        "fieldType": "text",
        "groupName": "contactinformation",
        "description": "Subject of the #nurture thread so later touches can reply with Re:.",
    },
    {
        "name": "gift_ideas",
        "label": "Gift ideas",
        "type": "string",
        "fieldType": "textarea",
        "groupName": "contactinformation",
    },
]

def _enum_options(values: tuple[str, ...] | list[str]) -> list[dict]:
    return [
        {"label": value.replace("_", " ").title(), "value": value, "displayOrder": i, "hidden": False}
        for i, value in enumerate(values)
    ]


DEAL_PROPS = [
    {
        "name": "crmbrain_locked",
        "label": "CRMBrain locked",
        "type": "bool",
        "fieldType": "booleancheckbox",
        "groupName": "dealinformation",
        "description": "When true, CRMBrain will not change stage or amount on this deal.",
    },
    {
        "name": "lost_reason",
        "label": "Lost reason",
        "type": "enumeration",
        "fieldType": "select",
        "groupName": "dealinformation",
        "options": _enum_options(LOST_REASONS),
    },
    {
        "name": "nurture_reason",
        "label": "Nurture reason",
        "type": "string",
        "fieldType": "textarea",
        "groupName": "dealinformation",
    },
    {
        "name": "nurture_thread_id",
        "label": "Nurture thread id",
        "type": "string",
        "fieldType": "text",
        "groupName": "dealinformation",
        "description": "Gmail thread started by the first #nurture email. Later nurture touches reply here only.",
    },
    {
        "name": "nurture_thread_subject",
        "label": "Nurture thread subject",
        "type": "string",
        "fieldType": "text",
        "groupName": "dealinformation",
    },
    {
        "name": "sg_deal_type",
        "label": "SG deal type",
        "type": "enumeration",
        "fieldType": "select",
        "groupName": "dealinformation",
        "options": _enum_options(SG_DEAL_TYPES),
    },
    {
        "name": "monthly_fee",
        "label": "Monthly fee",
        "type": "number",
        "fieldType": "number",
        "groupName": "dealinformation",
    },
    {
        "name": "contract_months",
        "label": "Contract months",
        "type": "number",
        "fieldType": "number",
        "groupName": "dealinformation",
    },
    {
        "name": "contract_end_date",
        "label": "Contract end date",
        "type": "date",
        "fieldType": "date",
        "groupName": "dealinformation",
    },
    {
        "name": "no_show_count",
        "label": "No-show count",
        "type": "number",
        "fieldType": "number",
        "groupName": "dealinformation",
    },
    {
        "name": "positive_replies_30d",
        "label": "Positive replies last 30 days",
        "type": "number",
        "fieldType": "number",
        "groupName": "dealinformation",
    },
    {
        "name": "josh_review_flag",
        "label": "Josh review flag",
        "type": "string",
        "fieldType": "text",
        "groupName": "dealinformation",
        "description": "Non-empty means Josh needs to review this deal.",
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
        for prop in DEAL_PROPS:
            resp = self._request(
                "GET", f"/crm/v3/properties/deals/{prop['name']}", retry=True, timeout=20
            )
            if resp.status_code == 404:
                created = self._request(
                    "POST", "/crm/v3/properties/deals", json=prop, timeout=WRITE_TIMEOUT
                )
                if created.status_code >= 400:
                    logger.warning("create deal prop %s: %s", prop["name"], created.text[:300])

    def _search(self, object_name: str, filters: list[dict], properties: list[str]) -> list[dict]:
        return self.search_objects(object_name, filters, properties, page_limit=10)

    def search_objects(
        self,
        object_name: str,
        filters: list[dict],
        properties: list[str],
        *,
        page_limit: int = 100,
        max_results: int = 500,
    ) -> list[dict]:
        out: list[dict] = []
        after = None
        while len(out) < max_results:
            payload: dict[str, Any] = {
                "filterGroups": [{"filters": filters}],
                "properties": properties,
                "limit": min(100, page_limit, max_results - len(out)),
            }
            if after:
                payload["after"] = after
            resp = self._request(
                "POST",
                f"/crm/v3/objects/{object_name}/search",
                json=payload,
                retry=True,
                timeout=READ_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            out.extend(data.get("results") or [])
            after = (data.get("paging") or {}).get("next", {}).get("after")
            if not after:
                break
        return out

    def contacts_for_deal(self, deal_id: str, properties: list[str] | None = None) -> list[dict]:
        if not deal_id:
            return []
        resp = self._request(
            "GET",
            f"/crm/v4/objects/deals/{deal_id}/associations/contacts",
            retry=True,
            timeout=READ_TIMEOUT,
        )
        if resp.status_code >= 400:
            return []
        ids = []
        for row in resp.json().get("results") or []:
            cid = row.get("toObjectId") or row.get("id")
            if cid:
                ids.append(str(cid))
        props = ",".join(
            properties
            or [
                "email",
                "firstname",
                "lastname",
                "phone",
                "company",
                "personal_details",
                "family_notes",
                "relationship_hooks",
                "pain_points",
                "jobtitle",
                "engagements_last_meeting_booked",
                "notes_last_contacted",
                "crm_source",
                "industry",
                "nurture_thread_id",
                "nurture_thread_subject",
            ]
        )
        contacts = []
        for cid in ids:
            c = self._request(
                "GET",
                f"/crm/v3/objects/contacts/{cid}",
                params={"properties": props},
                retry=True,
                timeout=20,
            )
            if c.ok:
                contacts.append(c.json())
        return contacts

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

    def find_contacts_by_name(self, name: str) -> list[dict]:
        """Every exact first+last match. Caller decides unique vs attach-to-richest."""
        parts = [p for p in (name or "").strip().split() if p]
        if len(parts) < 2:
            return []
        first, last = parts[0], " ".join(parts[1:])
        return self._search(
            "contacts",
            [
                {"propertyName": "firstname", "operator": "EQ", "value": first},
                {"propertyName": "lastname", "operator": "EQ", "value": last},
            ],
            CONTACT_SEARCH_PROPS,
        )

    def _find_contact_by_name(self, name: str) -> dict | None:
        """Exact first+last match. Skip if zero or multiple hits."""
        rows = self.find_contacts_by_name(name)
        return rows[0] if len(rows) == 1 else None

    def find_contacts_fuzzy(self, name: str = "", company: str = "") -> list[dict]:
        """Every company plus fuzzy person-name hit."""
        raw_company = (company or "").strip()
        raw_name = (name or "").strip()
        if not raw_name or len(raw_company) < 3:
            return []
        rows = self._search(
            "contacts",
            [{"propertyName": "company", "operator": "CONTAINS_TOKEN", "value": raw_company}],
            CONTACT_SEARCH_PROPS,
        )
        hits = []
        for row in rows:
            props = row.get("properties") or {}
            full = f"{props.get('firstname') or ''} {props.get('lastname') or ''}".strip()
            if names_fuzzy_match(raw_name, full):
                hits.append(row)
        return hits

    def find_contact_fuzzy(self, name: str = "", company: str = "") -> dict | None:
        """Company plus fuzzy person name (MacAntosh / McAntosh at Emcor)."""
        hits = self.find_contacts_fuzzy(name, company)
        return hits[0] if len(hits) == 1 else None

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
        existing = policy.resolve_engagement_contact(self, ev)
        if not existing and (ev.extra or {}).get("name_ambiguous"):
            return {"id": "", "properties": {}, "skipped": "ambiguous_name"}
        if not existing and (ev.extra or {}).get("non_person"):
            return {"id": "", "properties": {}, "skipped": "non_person"}
        if not existing:
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
                        "hs_mrr,hs_arr,hs_acv,hs_tcv,hs_is_closed_won,"
                        "hs_lastmodifieddate,hs_updated_by_user_id,description,"
                        "crmbrain_locked"
                    ),
                    "propertiesWithHistory": "dealstage,amount",
                },
                retry=True,
                timeout=20,
            )
            if d.ok:
                deals.append(d.json())
        return deals

    def _archive_duplicate_deals(self, deals: list[dict], ev: Engagement | None = None) -> list[dict]:
        """Soft-archive same-stage, no-amount duplicates. Keep the richer deal."""
        from crmbrain.deal_write import authorize_deal_lifecycle, record_lifecycle_refusal

        archived_ids: set[str] = set()
        report = getattr(self, "report", None)
        for _keep, dup in policy.duplicate_open_deal_pairs(deals):
            dup_id = str(dup.get("id") or "")
            if not dup_id or dup_id in archived_ids:
                continue
            ok, reason = authorize_deal_lifecycle(
                dup, ev=ev, settings=self.settings, action="archive"
            )
            if not ok:
                record_lifecycle_refusal(dup, reason, "archive", report=report)
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
        """Removed: amount-only matching created wrong deals. Always None."""
        del amount
        return None

    def deals_for_company(self, company: str) -> list[dict]:
        """Open deals on contacts whose company matches. Used for Paid-client gates."""
        raw = (company or "").strip()
        if len(raw) < 3:
            return []
        rows = self._search(
            "contacts",
            [{"propertyName": "company", "operator": "CONTAINS_TOKEN", "value": raw}],
            CONTACT_SEARCH_PROPS,
        )
        deals: list[dict] = []
        seen: set[str] = set()
        for row in rows[:20]:
            cid = str(row.get("id") or "")
            if not cid:
                continue
            for deal in self.open_deals_for_contact(cid):
                did = str(deal.get("id") or "")
                if did and did not in seen:
                    seen.add(did)
                    deals.append(deal)
        return deals

    def find_contact_for_commerce(
        self, name: str = "", company: str = "", amount: str = "", email: str = ""
    ) -> dict | None:
        """Payer email or exact payer-name match only. No company or amount match."""
        del company, amount
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
            return self._find_contact_by_name(name)
        return None

    def _apply_live_deal(self, deal: dict, ev: Engagement, stage: str, amount: str, contact: dict) -> dict:
        if _refused_dealstage(stage):
            logger.info("refuse dealstage write %s", stage)
            stage = ""
        current = (deal.get("properties") or {}).get("dealstage") or ""
        target = (
            policy.choose_deal_action(current, stage, ev, deal=deal, settings=self.settings)
            if stage
            else None
        )
        current_name = (deal.get("properties") or {}).get("dealname") or ""
        wanted = policy.deal_name_for(ev, contact)
        cleaned = policy.prefer_deal_name(
            policy.clean_deal_name(current_name, fallback=wanted),
            wanted,
        )
        if policy.is_weak_deal_name(current_name) and wanted:
            cleaned = wanted
        if target:
            from crmbrain.deal_write import authorize_deal_lifecycle, record_lifecycle_refusal

            ok, reason = authorize_deal_lifecycle(
                deal, ev=ev, settings=self.settings, action="move"
            )
            if not ok:
                record_lifecycle_refusal(deal, reason, "move", report=getattr(self, "report", None))
            else:
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
        if _refused_dealstage(stage):
            logger.info("refuse dealstage write %s", stage)
            return {}
        if self._is_excluded(ev, contact):
            logger.info("skip hubspot deal write for excluded person")
            return {}
        contact_id = contact["id"]
        existing = self._archive_duplicate_deals(self.open_deals_for_contact(contact_id), ev=ev)
        live = policy.live_open_deals(existing)
        if live:
            deal = max(live, key=policy.deal_richness)
            return self._apply_live_deal(deal, ev, stage, amount, contact)
        won = policy.closed_won_deals(existing)
        if won and stage == STAGE["paid"]:
            deal = max(won, key=policy.deal_richness)
            return self._apply_live_deal(deal, ev, stage, amount, contact)
        if won and not policy.is_new_completed_paperwork(ev):
            logger.info("skip new deal; contact already has Paid/Signed")
            return {}
        if policy.blocks_no_show_create(existing, stage):
            return {}
        target = policy.choose_deal_action(None, stage, ev, settings=self.settings) if stage else None
        if not target:
            return {}
        # HubSpot workflows can create a deal between the first read and POST.
        existing = self._archive_duplicate_deals(self.open_deals_for_contact(contact_id), ev=ev)
        live = policy.live_open_deals(existing)
        if live:
            deal = max(live, key=policy.deal_richness)
            return self._apply_live_deal(deal, ev, stage, amount, contact)
        won = policy.closed_won_deals(existing)
        if won and stage == STAGE["paid"]:
            deal = max(won, key=policy.deal_richness)
            return self._apply_live_deal(deal, ev, stage, amount, contact)
        if won and not policy.is_new_completed_paperwork(ev):
            logger.info("skip new deal; contact already has Paid/Signed")
            return {}
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
        if ev and not policy.may_mutate_existing_deal(ev, deal, self.settings):
            return False
        if not ev and policy.deal_is_locked(deal):
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

    def create_pipeline_deal(self, contact_id: str, properties: dict[str, Any]) -> dict:
        """Create a deal on an explicit pipeline (renewals). Refuses deleted stages."""
        from crmbrain.config import RENEWAL_PIPELINE

        stage = str(properties.get("dealstage") or "")
        if _refused_dealstage(stage):
            logger.warning("refuse dealstage write %s", stage)
            return {}
        props = {k: v for k, v in properties.items() if v not in (None, "")}
        props.setdefault("pipeline", RENEWAL_PIPELINE)
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
        return resp.json()

    def patch_deal(self, deal_id: str, properties: dict[str, Any]) -> None:
        properties = {k: v for k, v in properties.items() if v not in (None, "")}
        if not properties:
            return
        if _refused_dealstage(str(properties.get("dealstage") or "")):
            logger.warning("refuse dealstage write %s", properties.get("dealstage"))
            properties = {k: v for k, v in properties.items() if k != "dealstage"}
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
        if _refused_dealstage(stage):
            logger.warning("refuse dealstage write %s", stage)
            return
        props = {"dealstage": stage}
        if dealname:
            props["dealname"] = dealname
        if stage == STAGE["closed_won"]:
            existing = _existing_closedate(self, deal_id)
            if existing:
                props["closedate"] = existing
        resp = self._request(
            "PATCH",
            f"/crm/v3/objects/deals/{deal_id}",
            json={"properties": props},
            timeout=WRITE_TIMEOUT,
        )
        resp.raise_for_status()

    def increment_no_show_count(self, deal: dict) -> int:
        return increment_no_show_count(self, deal)

    def set_josh_review_flag(self, deal: dict, reason: str) -> None:
        set_josh_review_flag(self, deal, reason)

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

    def last_meeting_at(self, contact_id: str):
        """Most recent HubSpot meeting start time for this contact, or None."""
        from datetime import datetime, timezone

        ids = self._meeting_association_ids(contact_id)
        latest = None
        for mid in ids:
            resp = self._request(
                "GET",
                f"/crm/v3/objects/meetings/{mid}",
                params={"properties": "hs_meeting_start_time,hs_timestamp,hs_meeting_title"},
                retry=True,
                timeout=20,
            )
            if resp.status_code >= 400:
                continue
            props = (resp.json() or {}).get("properties") or {}
            raw = props.get("hs_meeting_start_time") or props.get("hs_timestamp")
            if not raw:
                continue
            try:
                if str(raw).isdigit():
                    stamp = datetime.fromtimestamp(int(raw) / 1000, tz=timezone.utc)
                else:
                    stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                    if stamp.tzinfo is None:
                        stamp = stamp.replace(tzinfo=timezone.utc)
            except (OSError, OverflowError, ValueError):
                continue
            if latest is None or stamp > latest:
                latest = stamp
        return latest

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


def _refused_dealstage(stage: str) -> bool:
    raw = (stage or "").strip()
    if not raw:
        return False
    return is_deleted_stage(raw) or raw in {NO_SHOW_HINT, "no_show", "increment_no_show_count"}


def _existing_closedate(hs: HubSpot, deal_id: str) -> str:
    try:
        resp = hs._request(
            "GET",
            f"/crm/v3/objects/deals/{deal_id}",
            params={"properties": "closedate"},
            retry=True,
            timeout=READ_TIMEOUT,
        )
        if resp.status_code >= 400:
            return ""
        return str(((resp.json() or {}).get("properties") or {}).get("closedate") or "").strip()
    except Exception:
        return ""


def increment_no_show_count(hs: HubSpot, deal: dict) -> int:
    """Increment no_show_count. Never changes dealstage."""
    if not deal or not deal.get("id"):
        return 0
    if policy.deal_is_locked(deal):
        return 0
    props = deal.setdefault("properties", {})
    try:
        current = int(float(str(props.get("no_show_count") or "0").strip() or "0"))
    except (TypeError, ValueError):
        current = 0
    new = current + 1
    hs.patch_deal(str(deal["id"]), {"no_show_count": str(new)})
    props["no_show_count"] = str(new)
    return new


def set_josh_review_flag(hs: HubSpot, deal: dict, reason: str) -> None:
    if not deal or not deal.get("id") or not reason:
        return
    if policy.deal_is_locked(deal):
        return
    hs.patch_deal(str(deal["id"]), {"josh_review_flag": reason})
    deal.setdefault("properties", {})["josh_review_flag"] = reason


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
