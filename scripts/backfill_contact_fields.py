#!/usr/bin/env python3
"""Fill empty HubSpot phones/emails and apply safe CRM deal cleanup.

Never overwrites a real email or phone. LeadMagic is charged only when a
value is found. Does not move deal stages.
"""
from __future__ import annotations

import json
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crmbrain.config import STAGE, Settings, is_client_context  # noqa: E402
from crmbrain.leadmagic import (  # noqa: E402
    find_email,
    find_mobile,
    looks_like_email,
    looks_like_phone,
    usable_linkedin,
)

JUNK_LI = "dnyanoba-mulgir"
BLANK_DEALS = ("344689944309", "344775829222", "344678917830", "344713141954")
STAGE_LABEL = {
    "appointmentscheduled": "Replied",
    "qualifiedtobuy": "Discovery Scheduled",
    "presentationscheduled": "Discovery Completed",
    "decisionmakerboughtin": "Proposal Sent",
    "closedwon": "Signed",
    "3482933986": "Paid",
    "3486952153": "Nurture",
    "3557889773": "No Show",
    "closedlost": "Closed Lost",
}

# Company-matched SmartLead rows already verified in campaignintelligence.
SMARTLEAD_EMAILS = {
    ("tabatha", "lucas"): "tabatha@sotexexteriors.com",
    ("troy", "marrs"): "troy@littlerockroofing.us",
    ("troy", "cruthers"): "tcruthers@therooftitan.com",
    ("daniel", "majure"): "daniel.majure@lastmilestrategies.com",
    ("brett", "huizenga"): "brett@hometownroofing.com",
    ("david", "daniel"): "david.daniel@roofsolutionstn.com",
    ("mark", "sokolowski"): "mark.sokolowski@sierraexperts.com",
}


def hs_headers(settings: Settings) -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.hubspot_token}", "Content-Type": "application/json"}


def hs_get(settings: Settings, path: str, **kw):
    r = requests.get(f"https://api.hubapi.com{path}", headers=hs_headers(settings), timeout=30, **kw)
    r.raise_for_status()
    return r.json()


def hs_patch(settings: Settings, path: str, payload: dict) -> dict:
    r = requests.patch(
        f"https://api.hubapi.com{path}", headers=hs_headers(settings), json=payload, timeout=30
    )
    r.raise_for_status()
    return r.json()


def list_objects(settings: Settings, object_name: str, properties: list[str]) -> list[dict]:
    out: list[dict] = []
    after = None
    while True:
        params = {"limit": 100, "properties": ",".join(properties)}
        if after:
            params["after"] = after
        data = hs_get(settings, f"/crm/v3/objects/{object_name}", params=params)
        out.extend(data.get("results") or [])
        after = (data.get("paging") or {}).get("next", {}).get("after")
        if not after:
            break
    return out


def attach_deal_contacts(settings: Settings, deals: list[dict]) -> list[dict]:
    for deal in deals:
        resp = requests.get(
            f"https://api.hubapi.com/crm/v4/objects/deals/{deal['id']}/associations/contacts",
            headers=hs_headers(settings),
            timeout=20,
        )
        ids = []
        if resp.ok:
            for row in resp.json().get("results") or []:
                cid = row.get("toObjectId") or row.get("id")
                if cid:
                    ids.append(int(cid) if str(cid).isdigit() else cid)
        deal["contact_ids"] = ids
    return deals


def contact_view(c: dict) -> dict:
    p = c.get("properties") or {}
    email = (p.get("email") or "").strip()
    phone = (p.get("phone") or "").strip()
    first = (p.get("firstname") or "").strip()
    last = (p.get("lastname") or "").strip()
    company = (p.get("company") or "").strip()
    linkedin = (p.get("hs_linkedin_url") or "").strip()
    domain = email.split("@")[1].lower() if "@" in email else ""
    return {
        "id": c["id"],
        "first": first,
        "last": last,
        "name": f"{first} {last}".strip(),
        "email": email,
        "phone": phone,
        "company": company,
        "linkedin": linkedin,
        "domain": domain,
        "need_email": not looks_like_email(email),
        "need_phone": not looks_like_phone(phone),
    }


def delete_blank_deals(settings: Settings) -> list[tuple[str, int]]:
    deleted = []
    for did in BLANK_DEALS:
        r = requests.delete(
            f"https://api.hubapi.com/crm/v3/objects/deals/{did}",
            headers=hs_headers(settings),
            timeout=20,
        )
        deleted.append((did, r.status_code))
    return deleted


def associate_orphans(settings: Settings, contacts: list[dict], deals: list[dict]) -> tuple[list, list]:
    by_name: dict[str, list[str]] = defaultdict(list)
    for c in contacts:
        name = f"{(c.get('properties') or {}).get('firstname') or ''} {(c.get('properties') or {}).get('lastname') or ''}".strip().lower()
        if name:
            by_name[name].append(c["id"])
    linked, skipped = [], []
    for d in deals:
        if d.get("contact_ids"):
            continue
        raw = ((d.get("properties") or {}).get("dealname") or "").split(" - ")[0].strip().lower()
        raw = re.sub(r"\s+", " ", raw)
        raw = re.sub(r"\s+deal$", "", raw).strip()
        if not raw or "goliath" in raw:
            skipped.append((d["id"], raw or "blank", "no unique person"))
            continue
        ids = by_name.get(raw) or []
        if len(ids) == 1:
            cid = ids[0]
            r = requests.put(
                f"https://api.hubapi.com/crm/v3/objects/deals/{d['id']}/associations/contacts/{cid}/3",
                headers=hs_headers(settings),
                timeout=20,
            )
            if r.status_code < 400:
                linked.append((d["id"], cid, raw))
            else:
                skipped.append((d["id"], raw, f"assoc {r.status_code}"))
        else:
            skipped.append((d["id"], raw, f"matches={len(ids)}"))
    return linked, skipped


def safe_patch(settings: Settings, rec: dict, email: str = "", phone: str = "", linkedin: str = "") -> dict:
    props = {}
    if email and rec["need_email"] and looks_like_email(email):
        props["email"] = email
    if phone and rec["need_phone"] and looks_like_phone(phone) and "*" not in phone:
        props["phone"] = phone
    li = usable_linkedin(linkedin)
    current = rec.get("linkedin") or ""
    if li and (not current or JUNK_LI in current.lower()):
        props["hs_linkedin_url"] = li
    if not props:
        return {}
    hs_patch(settings, f"/crm/v3/objects/contacts/{rec['id']}", {"properties": props})
    return props


def audit_deals(contacts: list[dict], deals: list[dict]) -> dict:
    by_id = {c["id"]: contact_view(c) for c in contacts}
    stages = Counter()
    orphans = []
    multi = []
    clients = []
    open_no_email = []
    for d in deals:
        p = d.get("properties") or {}
        stage = p.get("dealstage") or ""
        stages[STAGE_LABEL.get(stage, stage)] += 1
        name = p.get("dealname") or ""
        ids = d.get("contact_ids") or []
        if not ids:
            orphans.append({"id": d["id"], "name": name, "stage": STAGE_LABEL.get(stage, stage)})
        if len(ids) > 1:
            multi.append({"id": d["id"], "name": name, "contacts": ids})
        if is_client_context(name, name, ""):
            clients.append({"id": d["id"], "name": name, "stage": STAGE_LABEL.get(stage, stage)})
        if stage not in {STAGE["closed_lost"], STAGE["paid"], STAGE["signed"]} and ids:
            people = [by_id.get(str(i)) or by_id.get(i) for i in ids]
            if people and all(person and person["need_email"] for person in people if person):
                open_no_email.append({"id": d["id"], "name": name})
    return {
        "deal_count": len(deals),
        "contact_count": len(contacts),
        "stages": dict(stages),
        "orphans": orphans,
        "multi_contact": multi,
        "client_deals": clients,
    }


def run() -> dict:
    settings = Settings.from_env()
    if not settings.hubspot_token:
        raise SystemExit("HUBSPOT_ACCESS_TOKEN missing")
    contacts = list_objects(
        settings,
        "contacts",
        ["firstname", "lastname", "email", "phone", "company", "hs_linkedin_url", "jobtitle"],
    )
    deals = attach_deal_contacts(
        settings,
        list_objects(settings, "deals", ["dealname", "dealstage", "pipeline", "amount", "closedate"]),
    )
    deleted = delete_blank_deals(settings)
    linked, assoc_skipped = associate_orphans(settings, contacts, deals)

    views = [contact_view(c) for c in contacts]
    patched = []
    skipped = []
    remaining = []

    for rec in views:
        if not rec["need_email"] and not rec["need_phone"] and usable_linkedin(rec["linkedin"]):
            continue
        props = {}
        key = (rec["first"].lower(), rec["last"].lower())
        if rec["need_email"] and key in SMARTLEAD_EMAILS:
            props.update(safe_patch(settings, rec, email=SMARTLEAD_EMAILS[key]) or {})
            if props.get("email"):
                rec["email"] = props["email"]
                rec["need_email"] = False
                rec["domain"] = rec["email"].split("@")[1]
        if rec["need_email"] and (rec["domain"] or rec["company"]):
            found = find_email(settings, rec["first"], rec["last"], rec["domain"], rec["company"])
            if found:
                more = safe_patch(settings, rec, email=found) or {}
                props.update(more)
                if more.get("email"):
                    rec["email"] = more["email"]
                    rec["need_email"] = False
                    rec["domain"] = rec["email"].split("@")[1]
        if rec["need_phone"] and rec["email"]:
            mobile = find_mobile(settings, rec["email"], rec["linkedin"])
            if mobile:
                more = safe_patch(settings, rec, phone=mobile) or {}
                props.update(more)
                if more.get("phone"):
                    rec["phone"] = more["phone"]
                    rec["need_phone"] = False
        if props:
            patched.append({"id": rec["id"], "name": rec["name"], **props})
        elif rec["need_email"] or rec["need_phone"]:
            remaining.append(rec)
        time.sleep(0.15)

    # Refresh after patches for leftover counts
    contacts = list_objects(
        settings,
        "contacts",
        ["firstname", "lastname", "email", "phone", "company", "hs_linkedin_url"],
    )
    deals = attach_deal_contacts(
        settings,
        list_objects(settings, "deals", ["dealname", "dealstage", "pipeline", "amount"]),
    )
    views = [contact_view(c) for c in contacts]
    report = {
        "deleted_blank_deals": deleted,
        "associated_orphans": [{"deal": a[0], "contact": a[1], "name": a[2]} for a in linked],
        "associate_skipped": [{"deal": a[0], "name": a[1], "why": a[2]} for a in assoc_skipped],
        "patched": patched,
        "patched_count": len(patched),
        "still_missing_email": [r for r in views if r["need_email"]],
        "still_missing_phone": [r for r in views if r["need_phone"]],
        "audit": audit_deals(contacts, deals),
    }
    out = Path("/tmp/enrich_report.json")
    out.write_text(json.dumps(report, indent=2))
    print(
        json.dumps(
            {
                "patched": len(patched),
                "missing_email": len(report["still_missing_email"]),
                "missing_phone": len(report["still_missing_phone"]),
                "orphans": len(report["audit"]["orphans"]),
                "stages": report["audit"]["stages"],
                "report": str(out),
            },
            indent=2,
        )
    )
    return report


if __name__ == "__main__":
    run()
