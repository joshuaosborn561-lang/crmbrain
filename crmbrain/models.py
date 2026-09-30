from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class Engagement:
    """Someone Josh actually talked to, or who reached back."""

    source: str
    external_id: str
    occurred_at: datetime | None = None
    first_name: str = ""
    last_name: str = ""
    name: str = ""
    email: str = ""
    phone: str = ""
    company: str = ""
    title: str = ""
    linkedin_url: str = ""
    domain: str = ""
    transcript: str = ""
    summary: str = ""
    raw_subject: str = ""
    stage_hint: str = ""
    ticker_reason: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def display_name(self) -> str:
        built = f"{self.first_name} {self.last_name}".strip()
        if built:
            return built
        if self.name:
            from crmbrain.names import looks_like_meeting_title

            if looks_like_meeting_title(self.name):
                return ""
            return self.name
        return ""


@dataclass
class CycleReport:
    processed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    contacts_upserted: list[str] = field(default_factory=list)
    deals_moved: list[str] = field(default_factory=list)
    deals_pruned: list[str] = field(default_factory=list)
    contacts_pruned: list[str] = field(default_factory=list)
    junk_blocked: list[str] = field(default_factory=list)
    ticker_enrolled: list[str] = field(default_factory=list)
    ticker_drafts: list[str] = field(default_factory=list)
    linkedin_queued: list[str] = field(default_factory=list)
    briefs_sent: list[str] = field(default_factory=list)
    notes_updated: list[str] = field(default_factory=list)
    amounts_set: list[str] = field(default_factory=list)
    deals_restored: list[str] = field(default_factory=list)
    review_queue: list[str] = field(default_factory=list)
    stale_sources: list[str] = field(default_factory=list)
    proposed_writes: list[Any] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    integrations: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    dry_run: bool = False
    calendar_api_ok: bool = True
    reconcile_aborted: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "integrations": self.integrations,
            "processed": self.processed,
            "skipped": self.skipped,
            "contacts_upserted": self.contacts_upserted,
            "deals_moved": self.deals_moved,
            "deals_pruned": self.deals_pruned,
            "contacts_pruned": self.contacts_pruned,
            "deals_restored": self.deals_restored,
            "junk_blocked": self.junk_blocked,
            "ticker_enrolled": self.ticker_enrolled,
            "ticker_drafts": self.ticker_drafts,
            "linkedin_queued": self.linkedin_queued,
            "briefs_sent": self.briefs_sent,
            "notes_updated": self.notes_updated,
            "amounts_set": self.amounts_set,
            "review_queue": self.review_queue,
            "stale_sources": self.stale_sources,
            "proposed_writes": self.proposed_writes,
            "warnings": self.warnings,
            "errors": self.errors,
            "dry_run": self.dry_run,
            "calendar_api_ok": self.calendar_api_ok,
            "reconcile_aborted": self.reconcile_aborted,
        }

    def summary_text(self) -> str:
        title = "CRM Brain cycle (dry-run)" if self.dry_run else "CRM Brain cycle"
        lines = [title]
        if self.reconcile_aborted:
            lines.append("reconcile_aborted: true")
        for key, values in self.as_dict().items():
            if not isinstance(values, list):
                continue
            lines.append(f"{key}: {len(values)}")
            for item in values[:20]:
                lines.append(f"  - {item}")
        return "\n".join(lines)


@dataclass
class IntentDecision:
    """Sales-opportunity classification for one person / meeting."""

    verdict: str  # yes | no | review
    intent: str = ""
    confidence: float = 0.0
    reason: str = ""
    stage: str = ""
    amount: str = ""


@dataclass
class ProposedWrite:
    """A HubSpot mutation the cycle would make (dry-run or live)."""

    action: str
    label: str
    stage: str = ""
    amount: str = ""
    contact_id: str = ""
    deal_id: str = ""
    reason: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "action": self.action,
            "label": self.label,
            "stage": self.stage,
            "amount": self.amount,
            "contact_id": self.contact_id,
            "deal_id": self.deal_id,
            "reason": self.reason,
        }

    def as_line(self) -> str:
        bits = [self.action, self.label]
        if self.stage:
            bits.append(self.stage)
        if self.amount:
            bits.append(f"${self.amount}")
        if self.deal_id:
            bits.append(f"deal:{self.deal_id}")
        if self.reason:
            bits.append(f"({self.reason})")
        return " ".join(bits)
