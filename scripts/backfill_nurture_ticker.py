#!/usr/bin/env python3
"""Enroll historical 90-day nurture ticker rows.

Dry-run by default. Pass --apply to write ticker rows. Never sends email.
Slack drafts still happen only on the next cycle via _fire_ticker.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crmbrain.config import STAGE, Settings, now_utc  # noqa: E402
from crmbrain.hubspot import HubSpot  # noqa: E402
from crmbrain.memory import Memory  # noqa: E402
from crmbrain.sources import smartlead  # noqa: E402
from crmbrain.ticker import (  # noqa: E402
    TickerCandidate,
    apply_plan,
    classify_reason,
    in_live_meeting_stage,
    parse_signal_at,
)


def _hs_headers(settings: Settings) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.hubspot_token}",
        "Content-Type": "application/json",
    }


def _search_deals(settings: Settings, stages: list[str]) -> list[dict]:
    out: list[dict] = []
    after = None
    while True:
        payload: dict = {
            "filterGroups": [
                {
                    "filters": [
                        {
                            "propertyName": "dealstage",
                            "operator": "IN",
                            "values": stages,
                        }
                    ]
                }
            ],
            "properties": [
                "dealname",
                "dealstage",
                "createdate",
                "hs_lastmodifieddate",
                "closedate",
            ],
            "limit": 100,
        }
        if after:
            payload["after"] = after
        resp = requests.post(
            "https://api.hubapi.com/crm/v3/objects/deals/search",
            headers=_hs_headers(settings),
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        out.extend(data.get("results") or [])
        after = (data.get("paging") or {}).get("next", {}).get("after")
        if not after:
            break
    return out


def _deal_contacts(settings: Settings, deal_id: str) -> list[dict]:
    resp = requests.get(
        f"https://api.hubapi.com/crm/v4/objects/deals/{deal_id}/associations/contacts",
        headers=_hs_headers(settings),
        timeout=20,
    )
    if resp.status_code >= 400:
        return []
    ids = []
    for row in resp.json().get("results") or []:
        cid = row.get("toObjectId") or row.get("id")
        if cid:
            ids.append(str(cid))
    contacts = []
    for cid in ids:
        c = requests.get(
            f"https://api.hubapi.com/crm/v3/objects/contacts/{cid}",
            headers=_hs_headers(settings),
            params={
                "properties": ",".join(
                    [
                        "email",
                        "firstname",
                        "lastname",
                        "phone",
                        "company",
                        "personal_details",
                        "family_notes",
                        "relationship_hooks",
                        "pain_points",
                    ]
                )
            },
            timeout=20,
        )
        if c.ok:
            contacts.append(c.json())
    return contacts


def collect_smartlead(settings: Settings) -> tuple[list[TickerCandidate], list[str]]:
    errors: list[str] = []
    if not settings.smartlead_key:
        return [], errors
    try:
        events = smartlead.scan(settings)
    except Exception as exc:
        errors.append(f"smartlead: {exc}")
        return [], errors
    now = now_utc()
    out: list[TickerCandidate] = []
    for ev in events:
        text = " ".join(x for x in (ev.summary, ev.transcript, ev.raw_subject) if x)
        reason = classify_reason(hint=ev.ticker_reason, text=text)
        if ev.source == "smartlead" and reason not in {"no_show", "kicked_can", "deal_died"}:
            reason = "never_booked"
        out.append(
            TickerCandidate(
                name=ev.display_name(),
                email=ev.email or "",
                phone=ev.phone or "",
                company=ev.company or "",
                reason=reason,
                last_signal=parse_signal_at(ev.occurred_at) or now,
                source="smartlead",
            )
        )
    return out, errors


def collect_hubspot(settings: Settings) -> tuple[list[TickerCandidate], list[str]]:
    errors: list[str] = []
    if not settings.hubspot_token:
        return [], errors
    try:
        deals = _search_deals(settings, [STAGE["nurture"], STAGE["no_show"]])
    except Exception as exc:
        errors.append(f"hubspot deals: {exc}")
        return [], errors
    now = now_utc()
    out: list[TickerCandidate] = []
    for deal in deals:
        props = deal.get("properties") or {}
        stage = props.get("dealstage") or ""
        last_signal = (
            parse_signal_at(props.get("hs_lastmodifieddate"))
            or parse_signal_at(props.get("closedate"))
            or parse_signal_at(props.get("createdate"))
            or now
        )
        try:
            contacts = _deal_contacts(settings, str(deal["id"]))
        except Exception as exc:
            errors.append(f"hubspot deal {deal.get('id')}: {exc}")
            continue
        if not contacts:
            name = (props.get("dealname") or "").split(" - ")[0].strip()
            out.append(
                TickerCandidate(
                    name=name,
                    reason=classify_reason(stage=stage, text=props.get("dealname") or ""),
                    last_signal=last_signal,
                    hs_deal_id=str(deal["id"]),
                    source="hubspot",
                )
            )
            continue
        for contact in contacts:
            cp = contact.get("properties") or {}
            text = " ".join(
                x
                for x in (
                    props.get("dealname"),
                    cp.get("personal_details"),
                    cp.get("family_notes"),
                    cp.get("relationship_hooks"),
                    cp.get("pain_points"),
                )
                if x
            )
            first = (cp.get("firstname") or "").strip()
            last = (cp.get("lastname") or "").strip()
            out.append(
                TickerCandidate(
                    name=f"{first} {last}".strip() or (props.get("dealname") or ""),
                    email=(cp.get("email") or "").strip(),
                    phone=(cp.get("phone") or "").strip(),
                    company=(cp.get("company") or "").strip(),
                    reason=classify_reason(stage=stage, text=text),
                    last_signal=last_signal,
                    hs_contact_id=str(contact.get("id") or ""),
                    hs_deal_id=str(deal["id"]),
                    source="hubspot",
                )
            )
    return out, errors


def drop_active_pipeline(
    candidates: list[TickerCandidate],
    hs: HubSpot | None,
) -> tuple[list[TickerCandidate], list[TickerCandidate]]:
    """Skip positives who already have a live meeting / won deal."""
    if not hs:
        return candidates, []
    kept: list[TickerCandidate] = []
    skipped: list[TickerCandidate] = []
    for c in candidates:
        if c.reason in {"no_show", "kicked_can"}:
            kept.append(c)
            continue
        try:
            contact = None
            if c.hs_contact_id:
                contact = {"id": c.hs_contact_id}
            elif c.email or c.phone:
                contact = hs.find_contact(email=c.email, phone=c.phone)
            if not contact:
                kept.append(c)
                continue
            c.hs_contact_id = c.hs_contact_id or str(contact.get("id") or "")
            deals = hs.open_deals_for_contact(contact["id"])
        except Exception:
            kept.append(c)
            continue
        stages = {(d.get("properties") or {}).get("dealstage") or "" for d in deals}
        if any(in_live_meeting_stage(s) for s in stages):
            c.skip_reason = "live_pipeline"
            skipped.append(c)
            continue
        if STAGE["no_show"] in stages:
            c.reason = "no_show"
        kept.append(c)
    return kept, skipped


def format_report(
    result: dict,
    source_counts: dict[str, int],
    collect_errors: list[str],
    pipeline_skipped: int,
    apply: bool,
) -> str:
    lines = [
        "Nurture ticker backfill",
        f"mode: {'apply' if apply else 'dry-run'}",
        f"sources: smartlead={source_counts.get('smartlead', 0)} hubspot={source_counts.get('hubspot', 0)}",
        f"already_on_ticker: {result['already_on_ticker']}",
        f"skipped_pipeline: {pipeline_skipped}",
        f"would_enroll: {result['would_enroll']}",
    ]
    for reason, count in sorted((result.get("by_reason") or {}).items()):
        lines.append(f"  {reason}: {count}")
    if apply:
        lines.append(f"enrolled: {result['enrolled']}")
    if collect_errors or result.get("errors"):
        lines.append("errors:")
        for err in collect_errors + list(result.get("errors") or []):
            lines.append(f"  - {err}")
    rows = result.get("rows") or []
    if rows:
        lines.append("candidates:")
        for row in rows[:40]:
            who = row.get("email") or row.get("phone") or row.get("name") or row.get("id")
            lines.append(
                f"  - {row.get('name') or '?'} <{who}> {row.get('reason')} next_fire={row.get('next_fire_at')}"
            )
        if len(rows) > 40:
            lines.append(f"  ... {len(rows) - 40} more")
    if not apply:
        lines.append(
            "Nothing written. Pass --apply to enroll. Slack drafts fire on the next cycle. No email is sent."
        )
    return "\n".join(lines)


def run(apply: bool = False, settings: Settings | None = None, memory: Memory | None = None) -> dict:
    settings = settings or Settings.from_env()
    memory = memory or Memory(settings)
    collect_errors: list[str] = []
    sl, sl_err = collect_smartlead(settings)
    collect_errors.extend(sl_err)
    hs_rows, hs_err = collect_hubspot(settings)
    collect_errors.extend(hs_err)
    source_counts = {
        "smartlead": len(sl),
        "hubspot": len(hs_rows),
    }
    hs = HubSpot(settings) if settings.hubspot_token else None
    merged, pipeline_skipped = drop_active_pipeline(sl + hs_rows, hs)
    result = apply_plan(memory, merged, write=apply)
    result["source_counts"] = source_counts
    result["collect_errors"] = collect_errors
    result["pipeline_skipped"] = len(pipeline_skipped)
    result["report"] = format_report(
        result, source_counts, collect_errors, len(pipeline_skipped), apply
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write ticker rows. Default is dry-run (print counts only).",
    )
    args = parser.parse_args()
    result = run(apply=args.apply)
    print(result["report"])
    if result.get("collect_errors") or result.get("errors"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
