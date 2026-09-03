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
        if self.name:
            return self.name
        return f"{self.first_name} {self.last_name}".strip()


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
    integrations: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, list[str]]:
        return {
            "integrations": self.integrations,
            "processed": self.processed,
            "skipped": self.skipped,
            "contacts_upserted": self.contacts_upserted,
            "deals_moved": self.deals_moved,
            "deals_pruned": self.deals_pruned,
            "contacts_pruned": self.contacts_pruned,
            "junk_blocked": self.junk_blocked,
            "ticker_enrolled": self.ticker_enrolled,
            "ticker_drafts": self.ticker_drafts,
            "linkedin_queued": self.linkedin_queued,
            "briefs_sent": self.briefs_sent,
            "notes_updated": self.notes_updated,
            "amounts_set": self.amounts_set,
            "errors": self.errors,
        }

    def summary_text(self) -> str:
        lines = ["CRM Brain cycle"]
        for key, values in self.as_dict().items():
            lines.append(f"{key}: {len(values)}")
            for item in values[:20]:
                lines.append(f"  - {item}")
        return "\n".join(lines)
