"""SalesGlider lead source (sg_lead_source) stamped on HubSpot contacts and deals at booking time.

Channel evidence, strongest first:
1. The engagement itself came from a channel (Smartlead reply, HeyReach chat, Allo call, RVM callback): confirmed.
2. The booking row in public.booking_events carries a Smartlead send_id (the prospect booked from a
   link in a cold email): Cold email, confirmed.
3. Earlier channel touches for the same email (crmbrain.ticker rows from smartlead / heyreach / rvm, or the
   contact's lead_source_platofrm): inferred.
Nothing matched means we leave the property blank. Referral, Live leads, Partner play and Inbound/other
are not detectable from these feeds, so they are only set by hand or by the backfill scripts.
An existing sg_lead_source is never overwritten.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

SOURCE_PROP = "sg_lead_source"
CONFIDENCE_PROP = "sg_lead_source_confidence"

REFERRAL = "Referral"
COLD_EMAIL = "Cold email"
LINKEDIN = "LinkedIn/HeyReach"
COLD_CALL = "Cold call"
LIVE_LEADS = "Live leads"
PARTNER_PLAY = "Partner play"
INBOUND_OTHER = "Inbound/other"
LEAD_SOURCES = (REFERRAL, COLD_EMAIL, LINKEDIN, COLD_CALL, LIVE_LEADS, PARTNER_PLAY, INBOUND_OTHER)
CONFIRMED = "confirmed"
INFERRED = "inferred"

# Engagement.source values that are themselves a channel.
_DIRECT = {
    "smartlead": COLD_EMAIL,
    "heyreach": LINKEDIN,
    "allo": COLD_CALL,
    "rvm": COLD_CALL,
}
# Earlier-touch markers (ticker.source, contact lead_source_platofrm), lowercased.
_HISTORY = {
    "smartlead": COLD_EMAIL,
    "heyreach": LINKEDIN,
    "rvm": COLD_CALL,
    "allo": COLD_CALL,
}


def _norm_email(email: str | None) -> str:
    return (email or "").strip().lower()


def _rows(memory: Any, schema: str, table: str, params: dict) -> list[dict]:
    if memory is None or not getattr(memory, "use_supabase", False):
        return []
    try:
        return list(memory._sb_named(schema, "GET", table, params=params) or [])
    except Exception as exc:  # evidence lookups must never break the cycle
        logger.warning("lead_source lookup %s.%s failed: %s", schema, table, exc)
        return []


def resolve_lead_source(ev: Any, memory: Any = None, contact: dict | None = None) -> tuple[str, str] | None:
    """Return (source, confidence) or None when there is no evidence."""
    direct = _DIRECT.get((getattr(ev, "source", "") or "").lower())
    if direct:
        return direct, CONFIRMED
    email = _norm_email(getattr(ev, "email", ""))
    if email:
        rows = _rows(
            memory,
            "public",
            "booking_events",
            {
                "select": "send_id",
                "lead_email": f"ilike.{email}",
                "event_type": "eq.booking_confirmed",
                "send_id": "not.is.null",
                "limit": "1",
            },
        )
        if rows:
            return COLD_EMAIL, CONFIRMED
        for row in _rows(
            memory,
            "crmbrain",
            "ticker",
            {"select": "source", "email": f"ilike.{email}", "limit": "20"},
        ):
            mapped = _HISTORY.get((row.get("source") or "").strip().lower())
            if mapped:
                return mapped, INFERRED
    platform = ((contact or {}).get("properties") or {}).get("lead_source_platofrm") or ""
    mapped = _HISTORY.get(platform.strip().lower())
    if mapped:
        return mapped, INFERRED
    return None
