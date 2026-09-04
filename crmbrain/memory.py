from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import requests

from crmbrain.config import Settings

logger = logging.getLogger(__name__)


def _is_duplicate_key(exc: BaseException) -> bool:
    """PostgREST unique violation (409) — row already exists."""
    msg = str(exc).lower()
    return (
        " 409" in f" {msg}"
        or msg.startswith("409")
        or "duplicate key" in msg
        or "unique constraint" in msg
        or "23505" in msg
        or ": 409" in msg
        or " 409:" in msg
    )


class Memory:
    """Idempotency + ticker. Supabase first, local JSON fallback.

    Supabase write/read failures are recorded on ``errors`` so the cycle
    report can surface them. Local JSON still updates so a cycle can finish.
    """

    def __init__(self, settings: Settings, data_dir: Path | None = None):
        self.settings = settings
        self.data_dir = data_dir or Path("data")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / "account_memory.json"
        self._local = self._load_local()
        self.use_supabase = bool(settings.supabase_url and settings.supabase_key)
        self.errors: list[str] = []

    def _load_local(self) -> dict[str, Any]:
        if self.path.exists():
            return json.loads(self.path.read_text())
        return {"processed": [], "ticker": [], "facts": [], "runs": []}

    def save_local(self) -> None:
        self.path.write_text(json.dumps(self._local, indent=2, default=str))

    def _record_error(self, op: str, exc: BaseException) -> None:
        msg = f"memory {op}: {exc}"
        logger.warning(msg)
        if msg not in self.errors:
            self.errors.append(msg)

    def drain_errors(self) -> list[str]:
        out = list(self.errors)
        self.errors.clear()
        return out

    def _sb(self, method: str, table: str, **kwargs) -> Any:
        if not self.use_supabase:
            return None
        url = f"{self.settings.supabase_url.rstrip('/')}/rest/v1/{table}"
        headers = {
            "apikey": self.settings.supabase_key,
            "Authorization": f"Bearer {self.settings.supabase_key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }
        resp = requests.request(method, url, headers=headers, timeout=30, **kwargs)
        if resp.status_code >= 400:
            raise RuntimeError(f"supabase {table} {resp.status_code}: {resp.text[:400]}")
        if not resp.text:
            return None
        return resp.json()

    def already_processed(self, source: str, external_id: str) -> bool:
        key = f"{source}:{external_id}"
        if key in self._local.get("processed", []):
            return True
        if self.use_supabase:
            try:
                rows = self._sb_schema(
                    "GET",
                    "processed_events",
                    params={
                        "source": f"eq.{source}",
                        "external_id": f"eq.{external_id}",
                        "select": "id",
                    },
                )
                return bool(rows)
            except Exception as exc:
                self._record_error("already_processed", exc)
                return False
        return False

    def _sb_schema(self, method: str, table: str, json_body: Any = None, params: dict | None = None) -> Any:
        url = f"{self.settings.supabase_url.rstrip('/')}/rest/v1/{table}"
        headers = {
            "apikey": self.settings.supabase_key,
            "Authorization": f"Bearer {self.settings.supabase_key}",
            "Content-Type": "application/json",
            "Accept-Profile": "crmbrain",
            "Content-Profile": "crmbrain",
            "Prefer": "return=representation,resolution=merge-duplicates",
        }
        resp = requests.request(
            method, url, headers=headers, timeout=30, json=json_body, params=params
        )
        if resp.status_code == 409:
            raise RuntimeError(f"supabase crmbrain.{table} 409: {resp.text[:400]}")
        if resp.status_code >= 400:
            raise RuntimeError(f"supabase crmbrain.{table} {resp.status_code}: {resp.text[:400]}")
        if not resp.content:
            return None
        return resp.json()

    def mark_processed(self, source: str, external_id: str, payload: dict | None = None) -> None:
        key = f"{source}:{external_id}"
        processed = self._local.setdefault("processed", [])
        if key not in processed:
            processed.append(key)
        self.save_local()
        if self.use_supabase:
            try:
                self._sb_schema(
                    "POST",
                    "processed_events",
                    json_body={"source": source, "external_id": external_id, "payload": payload or {}},
                )
            except Exception as exc:
                if _is_duplicate_key(exc):
                    logger.info("processed_events already had %s:%s", source, external_id)
                    return
                self._record_error("mark_processed", exc)

    def start_run(self) -> int | None:
        if not self.use_supabase:
            return None
        try:
            rows = self._sb_schema("POST", "cycle_runs", json_body={"status": "running"})
            if rows:
                return rows[0]["id"]
        except Exception as exc:
            self._record_error("start_run", exc)
            return None
        return None

    def finish_run(self, run_id: int | None, status: str, report: dict) -> None:
        self._local.setdefault("runs", []).append({"status": status, "report": report})
        self.save_local()
        if self.use_supabase and run_id is not None:
            try:
                self._sb_schema(
                    "PATCH",
                    "cycle_runs",
                    json_body={"status": status, "report": report, "finished_at": "now()"},
                    params={"id": f"eq.{run_id}"},
                )
            except Exception as exc:
                self._record_error("finish_run", exc)

    def enroll_ticker(self, row: dict) -> None:
        if self._ticker_already_active(row):
            return
        self._local.setdefault("ticker", []).append(row)
        self.save_local()
        if self.use_supabase:
            try:
                self._sb_schema("POST", "ticker", json_body=row)
            except Exception as exc:
                if _is_duplicate_key(exc):
                    logger.info("ticker already active for %s", row.get("email") or row.get("id"))
                    return
                self._record_error("enroll_ticker", exc)

    def _ticker_already_active(self, row: dict) -> bool:
        email = (row.get("email") or "").strip().lower()
        hs = str(row.get("hs_contact_id") or "").strip()
        for existing in self._local.get("ticker", []):
            if (existing.get("status") or "active") != "active":
                continue
            if email and (existing.get("email") or "").strip().lower() == email:
                return True
            if hs and str(existing.get("hs_contact_id") or "").strip() == hs:
                return True
        return False

    def list_ticker(self) -> list[dict]:
        local = list(self._local.get("ticker", []))
        if self.use_supabase:
            try:
                rows = self._sb_schema("GET", "ticker", params={"select": "*"})
                return rows if rows is not None else local
            except Exception as exc:
                self._record_error("list_ticker", exc)
                return local
        return local

    def due_ticker(self, now_iso: str) -> list[dict]:
        local = [
            t
            for t in self._local.get("ticker", [])
            if t.get("status") == "active" and t.get("next_fire_at", "") <= now_iso
        ]
        if self.use_supabase:
            try:
                rows = self._sb_schema(
                    "GET",
                    "ticker",
                    params={
                        "status": "eq.active",
                        "next_fire_at": f"lte.{now_iso}",
                        "select": "*",
                    },
                )
                return rows or local
            except Exception as exc:
                self._record_error("due_ticker", exc)
                return local
        return local

    def bump_ticker(self, ticker_id: str, next_fire_at: str, last_fired_at: str) -> None:
        for t in self._local.get("ticker", []):
            if str(t.get("id")) == str(ticker_id) or (
                t.get("email") and t.get("email") == ticker_id
            ):
                t["next_fire_at"] = next_fire_at
                t["last_fired_at"] = last_fired_at
        self.save_local()
        if self.use_supabase:
            try:
                self._sb_schema(
                    "PATCH",
                    "ticker",
                    json_body={"next_fire_at": next_fire_at, "last_fired_at": last_fired_at},
                    params={"id": f"eq.{ticker_id}"},
                )
            except Exception as exc:
                self._record_error("bump_ticker", exc)

    def stop_ticker(self, email: str | None = None, hs_contact_id: str | None = None) -> None:
        for t in self._local.get("ticker", []):
            if email and t.get("email") == email:
                t["status"] = "stopped"
            if hs_contact_id and t.get("hs_contact_id") == hs_contact_id:
                t["status"] = "stopped"
        self.save_local()
        if self.use_supabase:
            try:
                if email:
                    self._sb_schema(
                        "PATCH",
                        "ticker",
                        json_body={"status": "stopped"},
                        params={"email": f"eq.{email}", "status": "eq.active"},
                    )
                if hs_contact_id:
                    self._sb_schema(
                        "PATCH",
                        "ticker",
                        json_body={"status": "stopped"},
                        params={"hs_contact_id": f"eq.{hs_contact_id}", "status": "eq.active"},
                    )
            except Exception as exc:
                self._record_error("stop_ticker", exc)

    def save_fact(self, fact: dict) -> None:
        self._local.setdefault("facts", []).append(fact)
        self.save_local()
        if self.use_supabase:
            try:
                self._sb_schema("POST", "relationship_facts", json_body=fact)
            except Exception as exc:
                self._record_error("save_fact", exc)
