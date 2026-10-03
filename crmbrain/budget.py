"""Per-cycle HubSpot write budgets. Over cap → review_queue reason 'cap'."""

from __future__ import annotations

from dataclasses import dataclass, field

from crmbrain.config import STAGE, Settings
from crmbrain.policy import STAGE_RANK

DEFAULT_MAX_ARCHIVES_REGRESSIONS = 10
DEFAULT_MAX_CREATES = 10
DEFAULT_MAX_STAGE_MOVES = 20
DEFAULT_MAX_AMOUNT_WRITES = 20
DEFAULT_MAX_CHANGE_FRACTION = 0.15


def _kind(action: str, current: str, target: str) -> str:
    if action in {"create", "restore"}:
        return "create"
    if action == "archive":
        return "archive_regression"
    if target in {STAGE["nurture"], STAGE["closed_lost"]}:
        return "archive_regression"
    if current and STAGE_RANK.get(target, 0) < STAGE_RANK.get(current, 0):
        return "archive_regression"
    if current in {STAGE["signed"], STAGE["proposal_sent"]} or target in {
        STAGE["signed"],
        STAGE["proposal_sent"],
    }:
        return "stage_move"
    return "stage_move"


@dataclass
class WriteBudget:
    max_creates: int = DEFAULT_MAX_CREATES
    max_archives_regressions: int = DEFAULT_MAX_ARCHIVES_REGRESSIONS
    max_stage_moves: int = DEFAULT_MAX_STAGE_MOVES
    max_amount_writes: int = DEFAULT_MAX_AMOUNT_WRITES
    max_change_fraction: float = DEFAULT_MAX_CHANGE_FRACTION
    creates: int = 0
    archives_regressions: int = 0
    stage_moves: int = 0
    amount_writes: int = 0
    aborted: bool = False
    abort_reason: str = ""
    planned: list[dict] = field(default_factory=list)

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "WriteBudget":
        if settings is None:
            return cls()
        return cls(
            max_creates=int(getattr(settings, "max_creates", DEFAULT_MAX_CREATES)),
            max_archives_regressions=int(
                getattr(settings, "max_archives_regressions", DEFAULT_MAX_ARCHIVES_REGRESSIONS)
            ),
            max_stage_moves=int(getattr(settings, "max_stage_moves", DEFAULT_MAX_STAGE_MOVES)),
            max_amount_writes=int(
                getattr(settings, "max_amount_writes", DEFAULT_MAX_AMOUNT_WRITES)
            ),
            max_change_fraction=float(
                getattr(settings, "max_change_fraction", DEFAULT_MAX_CHANGE_FRACTION)
            ),
        )

    def classify(self, action: str, current: str = "", target: str = "") -> str:
        return _kind(action, current, target)

    def remaining(self, kind: str) -> int:
        if kind == "create":
            return max(0, self.max_creates - self.creates)
        if kind == "archive_regression":
            return max(0, self.max_archives_regressions - self.archives_regressions)
        if kind == "amount":
            return max(0, self.max_amount_writes - self.amount_writes)
        return max(0, self.max_stage_moves - self.stage_moves)

    def allow(self, kind: str) -> bool:
        if self.aborted:
            return False
        if kind == "create":
            if self.creates >= self.max_creates:
                return False
            self.creates += 1
            return True
        if kind == "archive_regression":
            if self.archives_regressions >= self.max_archives_regressions:
                return False
            self.archives_regressions += 1
            return True
        if kind == "amount":
            if self.amount_writes >= self.max_amount_writes:
                return False
            self.amount_writes += 1
            return True
        if self.stage_moves >= self.max_stage_moves:
            return False
        self.stage_moves += 1
        return True

    def maybe_abort(self, would_change: int, open_deals: int) -> bool:
        if open_deals <= 0 or would_change <= 0:
            return False
        if (would_change / open_deals) > self.max_change_fraction:
            self.aborted = True
            self.abort_reason = (
                f"reconcile abort: {would_change}/{open_deals} open deals "
                f"({would_change / open_deals:.0%}) would change (cap {self.max_change_fraction:.0%})"
            )
            return True
        return False
