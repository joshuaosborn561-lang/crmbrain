"""Per-person evidence timeline from every source in the cycle."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from crmbrain.config import STAGE, digits_phone
from crmbrain.models import Engagement
from crmbrain.policy import is_meeting_held, is_meeting_scheduled

KIND_BOOKED = "meeting_booked"
KIND_HELD = "meeting_held"
KIND_CANCELED = "meeting_canceled"
KIND_NO_SHOW = "meeting_no_show"
KIND_PROPOSAL = "proposal"
KIND_SIGNED = "agreement_completed"
KIND_PAYMENT = "payment"
KIND_POC = "poc_active"
KIND_REPLY = "reply_only"
KIND_PHONE = "phone_call"


@dataclass
class EvidenceEvent:
    source: str
    kind: str
    occurred_at: datetime | None = None
    title: str = ""
    text: str = ""
    amount: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class PersonTimeline:
    key: str
    email: str = ""
    phone: str = ""
    first_name: str = ""
    last_name: str = ""
    name: str = ""
    company: str = ""
    events: list[EvidenceEvent] = field(default_factory=list)
    engagements: list[Engagement] = field(default_factory=list)
    contact: dict | None = None
    deals: list[dict] = field(default_factory=list)

    def display_name(self) -> str:
        built = f"{self.first_name} {self.last_name}".strip()
        return built or self.name or self.email or self.phone or self.key

    def kinds(self) -> set[str]:
        return {e.kind for e in self.events}

    def latest(self, kind: str) -> EvidenceEvent | None:
        hits = [e for e in self.events if e.kind == kind]
        if not hits:
            return None
        return max(hits, key=lambda e: e.occurred_at or datetime.min.replace(tzinfo=None))

    def amount(self) -> str:
        for event in sorted(self.events, key=lambda e: e.occurred_at or datetime.min.replace(tzinfo=None), reverse=True):
            if event.amount:
                return event.amount
        return ""


def person_key(email: str = "", phone: str = "", name: str = "") -> str:
    if email:
        return f"email:{(email or '').strip().lower()}"
    digits = digits_phone(phone)
    if len(digits) >= 10:
        return f"phone:{digits[-10:]}"
    named = " ".join((name or "").lower().split())
    if named:
        return f"name:{named}"
    return ""


def kind_for(ev: Engagement) -> str:
    extra = ev.extra or {}
    if extra.get("canceled") or extra.get("cancelled"):
        return KIND_CANCELED
    if ev.stage_hint == STAGE["no_show"]:
        return KIND_NO_SHOW
    if ev.stage_hint == STAGE["paid"]:
        return KIND_PAYMENT
    if ev.stage_hint == STAGE["signed"]:
        return KIND_SIGNED
    if ev.stage_hint == STAGE["proposal_sent"]:
        return KIND_PROPOSAL
    blob = f"{ev.raw_subject} {ev.summary} {ev.transcript}".lower()
    if any(h in blob for h in ("poc", "proof of concept", "pilot", "kickoff", "onboarding")):
        if ev.source not in {"smartlead", "heyreach", "rvm"}:
            return KIND_POC
    if is_meeting_held(ev):
        return KIND_HELD
    if is_meeting_scheduled(ev) or extra.get("gcal_create") or extra.get("create_new"):
        return KIND_BOOKED
    if ev.source in {"smartlead", "heyreach", "rvm"}:
        return KIND_REPLY
    if ev.source == "allo":
        return KIND_PHONE
    return KIND_REPLY


def add_engagement(timelines: dict[str, PersonTimeline], ev: Engagement) -> PersonTimeline | None:
    key = person_key(ev.email, ev.phone, ev.display_name() or ev.name)
    if not key:
        return None
    row = timelines.get(key)
    if row is None:
        row = PersonTimeline(
            key=key,
            email=(ev.email or "").strip().lower(),
            phone=ev.phone,
            first_name=ev.first_name,
            last_name=ev.last_name,
            name=ev.display_name() or ev.name,
            company=ev.company,
        )
        timelines[key] = row
    else:
        row.email = row.email or (ev.email or "").strip().lower()
        row.phone = row.phone or ev.phone
        row.first_name = row.first_name or ev.first_name
        row.last_name = row.last_name or ev.last_name
        row.name = row.name or ev.display_name() or ev.name
        row.company = row.company or ev.company
    row.engagements.append(ev)
    extra = ev.extra or {}
    row.events.append(
        EvidenceEvent(
            source=ev.source,
            kind=kind_for(ev),
            occurred_at=ev.occurred_at,
            title=ev.raw_subject or extra.get("event_type") or "",
            text=(ev.summary or ev.transcript or "")[:2000],
            amount=str(extra.get("amount") or ""),
            extra=extra,
        )
    )
    return row


def build_timelines(engagements: list[Engagement]) -> dict[str, PersonTimeline]:
    timelines: dict[str, PersonTimeline] = {}
    for ev in engagements:
        add_engagement(timelines, ev)
    return timelines


def has_meeting_evidence(timeline: PersonTimeline) -> bool:
    return bool(timeline.kinds() & {KIND_BOOKED, KIND_HELD, KIND_POC, KIND_PROPOSAL, KIND_SIGNED, KIND_PAYMENT})


def reply_only(timeline: PersonTimeline) -> bool:
    kinds = timeline.kinds()
    return bool(kinds) and kinds <= {KIND_REPLY, KIND_PHONE} and KIND_HELD not in kinds and KIND_BOOKED not in kinds
