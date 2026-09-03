from __future__ import annotations

import json
import re
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from io import BytesIO
from typing import Iterable

import requests

from crmbrain.config import Settings, is_personal, today_and_yesterday_cdt
from crmbrain.http_mcp import extract_drive_ids, retry
from crmbrain.models import Engagement
from crmbrain.policy import looks_like_html

UA = {"User-Agent": "Mozilla/5.0 CRMBrain/0.1"}
DOCX_NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}


def _folder_html(folder_id: str) -> str:
    url = f"https://drive.google.com/drive/folders/{folder_id}"
    return retry(lambda: requests.get(url, headers=UA, timeout=40).text)


def _download_bytes(file_id: str) -> bytes:
    url = f"https://drive.google.com/uc?export=download&id={file_id}"

    def fetch() -> bytes:
        resp = requests.get(url, headers=UA, timeout=60, allow_redirects=True)
        resp.raise_for_status()
        if "text/html" in resp.headers.get("content-type", "") and "confirm" in resp.text:
            confirm = re.search(r"confirm=([0-9A-Za-z_]+)", resp.text)
            if confirm:
                resp = requests.get(
                    f"{url}&confirm={confirm.group(1)}", headers=UA, timeout=60
                )
        return resp.content

    return retry(fetch)


def _download_text(file_id: str) -> str:
    return _download_bytes(file_id).decode("utf-8", errors="replace")


def docx_text(content: bytes) -> str:
    """Pull plain text from a call-transcriber .docx (zip + word/document.xml)."""
    with zipfile.ZipFile(BytesIO(content)) as zf:
        xml = zf.read("word/document.xml")
    root = ET.fromstring(xml)
    parts: list[str] = []
    for node in root.findall(".//w:t", DOCX_NS):
        if node.text:
            parts.append(node.text)
        if node.tail:
            parts.append(node.tail)
    return "\n".join(p for p in parts if p).strip()


def _guess_name_from_meta(meta: dict) -> tuple[str, str, str]:
    callee = meta.get("callee") or meta.get("name") or meta.get("contact") or ""
    phone = str(meta.get("number") or meta.get("phone") or meta.get("callee_number") or "")
    direction = str(meta.get("direction") or "")
    return str(callee), phone, direction


def _nearby_window(html: str, fid: str, before: int = 400, after: int = 1200) -> str:
    idx = html.find(fid)
    if idx < 0:
        return ""
    return html[max(0, idx - before) : idx + after]


def file_kind(window: str) -> str:
    w = (window or "").lower()
    if "transcript.docx" in w or (".docx" in w and "transcript" in w):
        return "docx_transcript"
    if ".docx" in w:
        return "docx"
    if any(ext in w for ext in (".txt", ".vtt", ".srt")):
        return "text"
    if ".json" in w:
        return "json"
    return ""


def _load_transcripts(html: str, file_ids: list[str]) -> list[tuple[str, str, str, str]]:
    """Return (file_id, text, window, kind). Prefer *-transcript.docx over HTML .txt."""
    docx_hits: list[tuple[str, str, str]] = []
    text_hits: list[tuple[str, str]] = []
    for fid in file_ids:
        window = _nearby_window(html, fid)
        kind = file_kind(window)
        if kind in {"docx_transcript", "docx"}:
            docx_hits.append((fid, kind, window))
        elif kind == "text":
            text_hits.append((fid, window))

    out: list[tuple[str, str, str, str]] = []
    docx_hits.sort(key=lambda row: 0 if row[1] == "docx_transcript" else 1)
    for fid, kind, window in docx_hits:
        try:
            text = docx_text(_download_bytes(fid))
        except Exception:
            continue
        if len(text.strip()) < 20:
            continue
        out.append((fid, text, window, kind))
    if out:
        return out
    for fid, window in text_hits:
        try:
            text = _download_text(fid)
        except Exception:
            continue
        if looks_like_html(text) or len(text.strip()) < 20:
            continue
        out.append((fid, text, window, "text"))
    return out


def scan(settings: Settings, dates: Iterable[str] | None = None) -> list[Engagement]:
    """Read Cube ACR Drive. Prefer call-transcriber *-transcript.docx. No AssemblyAI."""
    dates = list(dates or today_and_yesterday_cdt())
    root_html = _folder_html(settings.cube_folder)
    # Dated subfolders appear as titles in the HTML next to file ids.
    folder_hits: dict[str, str] = {}
    for date in dates:
        # Find an id near the date label.
        for match in re.finditer(re.escape(date), root_html):
            window = root_html[max(0, match.start() - 800) : match.end() + 200]
            ids = extract_drive_ids(window)
            if ids:
                folder_hits[date] = ids[0]
                break
        if date not in folder_hits:
            # Sometimes the folder id is in data-id and the name is later.
            pattern = rf'data-id="([A-Za-z0-9_-]{{20,}})"[^>]*>.*?{re.escape(date)}'
            m = re.search(pattern, root_html, re.S)
            if m:
                folder_hits[date] = m.group(1)

    engagements: list[Engagement] = []
    for date, folder_id in folder_hits.items():
        html = _folder_html(folder_id)
        file_ids = extract_drive_ids(html)
        metas: dict[str, dict] = {}
        for fid in file_ids:
            window = _nearby_window(html, fid)
            if file_kind(window) == "json":
                try:
                    metas[fid] = json.loads(_download_text(fid))
                except Exception:
                    continue

        for fid, text, nearby, kind in _load_transcripts(html, file_ids):
            phone_match = re.search(r"\+?1?\d{10,11}", nearby)
            phone = phone_match.group(0) if phone_match else ""
            name_match = re.search(r"(20\d{2}-\d{2}-\d{2}[^<]{0,80})", nearby)
            label = name_match.group(1) if name_match else f"Cube ACR {date}"
            if is_personal(name=label, phone=phone):
                continue
            engagements.append(
                Engagement(
                    source="cube_acr",
                    external_id=fid,
                    occurred_at=datetime.fromisoformat(date).replace(tzinfo=timezone.utc),
                    name="",
                    phone=phone,
                    transcript=text[:20000],
                    summary=text[:800],
                    raw_subject=label,
                    extra={"folder_date": date, "transcript_kind": kind},
                )
            )

        # Metadata-only calls with no transcript get flagged, not transcribed.
        for fid, meta in metas.items():
            name, phone, direction = _guess_name_from_meta(meta)
            if is_personal(name=name, phone=phone):
                continue
            # Only emit if we did not already capture a transcript for this call.
            if any(e.phone and e.phone[-10:] == digits_last(phone) for e in engagements):
                continue
            engagements.append(
                Engagement(
                    source="cube_acr_meta",
                    external_id=fid,
                    occurred_at=datetime.fromisoformat(date).replace(tzinfo=timezone.utc),
                    name=name,
                    phone=phone,
                    summary=f"Call {direction} {name} {phone} on {date}. No transcript file in Drive.",
                    extra={"needs_transcript": True, "meta": meta},
                )
            )
    return engagements


def digits_last(phone: str) -> str:
    d = "".join(c for c in phone if c.isdigit())
    return d[-10:] if len(d) >= 10 else d
