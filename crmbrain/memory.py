from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from crmbrain.config import Settings, now_utc

logger = logging.getLogger(__name__)


def _run_started_stamp(row: dict | None) -> datetime | None:
    if not row:
        return None
    raw = row.get("started_at") or ""
    if not raw:
        return None
    try:
        stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def _overflow_email(row: dict | None) -> str:
    return str((row or {}).get("email") or "").strip().lower()


def _overflow_extra(raw: dict | None) -> dict:
    extra = (raw or {}).get("extra")
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except (TypeError, ValueError):
            extra = {}
    return extra if isinstance(extra, dict) else {}


def _overflow_from_row(row: dict | None) -> dict:
    raw = dict(row or {})
    extra = _overflow_extra(raw)
    return {
        "email": _overflow_email(raw),
        "external_id": str(raw.get("external_id") or ""),
        "first_name": str(raw.get("first_name") or ""),
        "last_name": str(raw.get("last_name") or ""),
        "name": str(raw.get("name") or ""),
        "domain": str(raw.get("domain") or ""),
        "company": str(raw.get("company") or ""),
        "raw_subject": str(raw.get("raw_subject") or ""),
        "summary": str(raw.get("summary") or ""),
        "occurred_at": raw.get("occurred_at"),
        "extra": extra,
    }


def _overflow_to_row(row: dict | None) -> dict:
    out = _overflow_from_row(row)
    out["extra"] = _overflow_extra(out)
    return out


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
        self.dry_run = bool(getattr(settings, "dry_run", False))
        self.errors: list[str] = []
        self._run_started_at = ""
        self.writes: list[str] = []

    def _load_local(self) -> dict[str, Any]:
        if self.path.exists():
            return json.loads(self.path.read_text())
        return {"processed": [], "ticker": [], "facts": [], "runs": []}

    def save_local(self, *, force: bool = False) -> None:
        if self.dry_run and not force:
            return
        self.path.write_text(json.dumps(self._local, indent=2, default=str))

    def _skip_side_write(self, op: str) -> bool:
        """Dry-run may only persist the cycle_runs report."""
        del op
        return bool(self.dry_run)

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
        if self._skip_side_write("processed_events"):
            return
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
        self._run_started_at = now_utc().isoformat()
        self.writes.append("cycle_runs")
        if not self.use_supabase:
            return None
        try:
            initial = "dry_run" if self.dry_run else "running"
            rows = self._sb_schema("POST", "cycle_runs", json_body={"status": initial})
            if rows:
                stamp = rows[0].get("started_at")
                if stamp:
                    self._run_started_at = str(stamp)
                return rows[0]["id"]
        except Exception as exc:
            self._record_error("start_run", exc)
            return None
        return None

    def last_finished_run_started_at(self) -> datetime | None:
        """Start time of the most recent ok/partial cycle_runs row."""
        if self.use_supabase:
            try:
                rows = self._sb_schema(
                    "GET",
                    "cycle_runs",
                    params={
                        "status": "in.(ok,partial)",
                        "select": "id,status,started_at,finished_at",
                        "order": "id.desc",
                        "limit": "1",
                    },
                )
                stamp = _run_started_stamp((rows or [None])[0] if rows else None)
                if stamp:
                    return stamp
            except Exception as exc:
                self._record_error("last_finished_run", exc)
        for row in reversed(self._local.get("runs") or []):
            if (row.get("status") or "") not in {"ok", "partial"}:
                continue
            stamp = _run_started_stamp(row)
            if stamp:
                return stamp
        return None

    def finish_run(self, run_id: int | None, status: str, report: dict) -> None:
        self._local.setdefault("runs", []).append(
            {
                "status": status,
                "started_at": self._run_started_at,
                "report": report,
            }
        )
        self.writes.append("cycle_runs")
        self.save_local(force=True)
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
        if self._skip_side_write("ticker"):
            return
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
        if self._skip_side_write("ticker"):
            return
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
        if self._skip_side_write("ticker"):
            return
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

    def _sb_named(self, schema: str, method: str, table: str, json_body: Any = None, params: dict | None = None) -> Any:
        url = f"{self.settings.supabase_url.rstrip('/')}/rest/v1/{table}"
        headers = {
            "apikey": self.settings.supabase_key,
            "Authorization": f"Bearer {self.settings.supabase_key}",
            "Content-Type": "application/json",
            "Accept-Profile": schema,
            "Content-Profile": schema,
            "Prefer": "return=representation,resolution=merge-duplicates",
        }
        resp = requests.request(
            method, url, headers=headers, timeout=30, json=json_body, params=params
        )
        if resp.status_code == 409:
            raise RuntimeError(f"supabase {schema}.{table} 409: {resp.text[:400]}")
        if resp.status_code >= 400:
            raise RuntimeError(f"supabase {schema}.{table} {resp.status_code}: {resp.text[:400]}")
        if not resp.content:
            return None
        return resp.json()

    def enqueue_review(self, row: dict) -> None:
        if self._skip_side_write("review_queue"):
            return
        row = dict(row)
        row.setdefault("status", "open")
        if self._review_already_open(row):
            return
        self._local.setdefault("review_queue", []).append(row)
        self.save_local()
        if self.use_supabase:
            try:
                self._sb_schema("POST", "review_queue", json_body=row)
            except Exception as exc:
                self._record_error("enqueue_review", exc)

    def _review_already_open(self, row: dict) -> bool:
        """Same person + reason stays one open review_queue row across cycles."""
        email = (row.get("email") or "").strip().lower()
        person = (row.get("person_key") or "").strip()
        reason = (row.get("reason") or "").strip()
        if not reason or not (email or person):
            return False
        for existing in self._local.get("review_queue") or []:
            if (existing.get("status") or "open") != "open":
                continue
            if (existing.get("reason") or "").strip() != reason:
                continue
            existing_email = (existing.get("email") or "").strip().lower()
            existing_person = (existing.get("person_key") or "").strip()
            if email and existing_email == email:
                return True
            if person and existing_person == person:
                return True
        if self.use_supabase:
            try:
                params = {
                    "reason": f"eq.{reason}",
                    "select": "id,email,person_key,status",
                    "limit": "20",
                }
                if person:
                    params["person_key"] = f"eq.{person}"
                elif email:
                    params["email"] = f"eq.{email}"
                for existing in self._sb_schema("GET", "review_queue", params=params) or []:
                    if (existing.get("status") or "open") == "open":
                        return True
            except Exception as exc:
                self._record_error("review_already_open", exc)
        return False

    def record_freshness(
        self,
        source: str,
        last_item_at: datetime | None = None,
        last_error: str = "",
        when: datetime | None = None,
        item_count: int | None = None,
    ) -> None:
        if self._skip_side_write("source_freshness"):
            return
        when = when or now_utc()
        if last_item_at is None:
            prior = (self._local.get("source_freshness") or {}).get(source) or {}
            raw = prior.get("last_item_at")
            if raw:
                try:
                    last_item_at = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                except ValueError:
                    last_item_at = None
            if last_item_at is None:
                last_item_at = self.latest_freshness(source)
        row = {
            "source": source,
            "last_item_at": last_item_at.isoformat() if last_item_at else None,
            "last_success_at": when.isoformat() if not last_error else None,
            "last_error": last_error or None,
            "item_count": item_count,
            "updated_at": when.isoformat(),
        }
        freshness = self._local.setdefault("source_freshness", {})
        freshness[source] = row
        self.save_local()
        if self.use_supabase:
            try:
                self._sb_schema("POST", "source_freshness", json_body=row)
            except Exception as exc:
                self._record_error("record_freshness", exc)

    def latest_freshness(self, source: str, *, fallback_local: bool = True) -> datetime | None:
        local = (self._local.get("source_freshness") or {}).get(source) or {}
        raw = local.get("last_item_at") if fallback_local else None
        if self.use_supabase:
            try:
                rows = self._sb_schema(
                    "GET",
                    "source_freshness",
                    params={"source": f"eq.{source}", "select": "last_item_at", "limit": "1"},
                )
                raw = (rows[0].get("last_item_at") if rows else None)
            except Exception as exc:
                self._record_error("latest_freshness", exc)
                if not fallback_local:
                    raise
        if not raw:
            return None
        try:
            stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return None
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)

    def latest_allo_call_at(self) -> datetime | None:
        if self.use_supabase:
            try:
                rows = self._sb_named(
                    "allo",
                    "GET",
                    "calls",
                    params={"select": "call_at", "order": "call_at.desc", "limit": "1"},
                )
                if rows:
                    raw = rows[0].get("call_at")
                    if raw:
                        stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                        return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
            except Exception as exc:
                self._record_error("latest_allo_call_at", exc)
        return None

    def list_allo_calls(self, since: datetime) -> list[dict]:
        if not self.use_supabase:
            return list(self._local.get("allo_calls") or [])
        try:
            return (
                self._sb_named(
                    "allo",
                    "GET",
                    "calls",
                    params={
                        "call_at": f"gte.{since.isoformat()}",
                        "select": "*",
                        "order": "call_at.desc",
                        "limit": "500",
                    },
                )
                or []
            )
        except Exception as exc:
            self._record_error("list_allo_calls", exc)
            return []

    def upsert_allo_call(self, row: dict) -> None:
        if self._skip_side_write("allo.calls"):
            return
        if not row.get("id"):
            return
        local = self._local.setdefault("allo_calls", [])
        local.append(row)
        self.save_local()
        if self.use_supabase:
            try:
                self._sb_named("allo", "POST", "calls", json_body=row)
            except Exception as exc:
                if _is_duplicate_key(exc):
                    return
                self._record_error("upsert_allo_call", exc)

    def upsert_cube_call(self, ev) -> None:
        if self._skip_side_write("cube_acr_calls"):
            return
        fid = getattr(ev, "external_id", None) or ""
        if not fid:
            return
        local = self._local.setdefault("cube_acr_calls", [])
        if any(str(row.get("id") or "") == str(fid) for row in local):
            return
        row = {
            "id": ev.external_id,
            "occurred_at": ev.occurred_at.isoformat() if ev.occurred_at else None,
            "name": ev.display_name(),
            "phone": ev.phone,
            "transcript": (ev.transcript or "")[:20000],
            "summary": (ev.summary or "")[:2000],
            "raw_subject": ev.raw_subject,
            "extra": ev.extra or {},
        }
        local.append(row)
        self.save_local()
        if self.use_supabase:
            try:
                self._sb_schema("POST", "cube_acr_calls", json_body=row)
            except Exception as exc:
                if _is_duplicate_key(exc):
                    return
                self._record_error("upsert_cube_call", exc)

    def save_fact(self, fact: dict) -> None:
        if self._skip_side_write("relationship_facts"):
            return
        self._local.setdefault("facts", []).append(fact)
        self.save_local()
        if self.use_supabase:
            try:
                self._sb_schema("POST", "relationship_facts", json_body=fact)
            except Exception as exc:
                self._record_error("save_fact", exc)

    def get_gmail_people_overflow(self) -> list[dict]:
        local = list(self._local.get("gmail_people_overflow") or [])
        if self.use_supabase:
            try:
                rows = self._sb_schema(
                    "GET",
                    "gmail_people_overflow",
                    params={"select": "*", "order": "occurred_at.desc.nullslast"},
                )
                if rows is not None:
                    cleaned = [_overflow_from_row(row) for row in rows]
                    self._local["gmail_people_overflow"] = cleaned
                    return cleaned
            except Exception as exc:
                self._record_error("get_gmail_people_overflow", exc)
                return local
        return local

    def upsert_gmail_people_overflow(self, rows: list[dict]) -> None:
        """Merge overflow by email. Never wipe the table before a write succeeds."""
        payload = [_overflow_from_row(row) for row in (rows or []) if _overflow_email(row)]
        current = {
            _overflow_email(row): _overflow_from_row(row)
            for row in self._local.get("gmail_people_overflow") or []
            if _overflow_email(row)
        }
        for row in payload:
            current[row["email"]] = row
        self._local["gmail_people_overflow"] = list(current.values())
        if self._skip_side_write("gmail_people_overflow"):
            return
        self.save_local()
        if self.use_supabase and payload:
            try:
                self._sb_schema(
                    "POST",
                    "gmail_people_overflow",
                    json_body=[_overflow_to_row(row) for row in payload],
                )
            except Exception as exc:
                self._record_error("upsert_gmail_people_overflow", exc)

    def set_gmail_people_overflow(self, rows: list[dict]) -> None:
        self.upsert_gmail_people_overflow(rows)

    def drop_gmail_people_overflow(self, email: str) -> None:
        key = (email or "").strip().lower()
        if not key:
            return
        self._local["gmail_people_overflow"] = [
            row
            for row in self._local.get("gmail_people_overflow") or []
            if _overflow_email(row) != key
        ]
        if self._skip_side_write("gmail_people_overflow"):
            return
        self.save_local()
        if self.use_supabase:
            try:
                self._sb_schema(
                    "DELETE",
                    "gmail_people_overflow",
                    params={"email": f"eq.{key}"},
                )
            except Exception as exc:
                self._record_error("drop_gmail_people_overflow", exc)
