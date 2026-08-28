from __future__ import annotations

import json
import re
import time
from typing import Any

import requests


class McpClient:
    """Minimal streamable-HTTP MCP client (JSON-RPC + SSE)."""

    def __init__(self, url: str, headers: dict[str, str] | None = None, timeout: int = 45):
        self.url = url
        self.headers = headers or {}
        self.timeout = timeout
        self.session_id = ""
        self._id = 0

    def _parse(self, text: str) -> Any:
        text = text.strip()
        if not text:
            return None
        if text.startswith("event:") or "data:" in text:
            chunks = []
            for line in text.splitlines():
                if line.startswith("data:"):
                    chunks.append(line[5:].strip())
            if chunks:
                return json.loads(chunks[-1])
        return json.loads(text)

    def initialize(self) -> Any:
        headers = {
            **self.headers,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "crmbrain", "version": "0.1.0"},
            },
        }
        resp = requests.post(self.url, headers=headers, json=payload, timeout=self.timeout)
        self.session_id = resp.headers.get("mcp-session-id") or resp.headers.get("Mcp-Session-Id") or ""
        body = self._parse(resp.text)
        notify_headers = {**headers}
        if self.session_id:
            notify_headers["mcp-session-id"] = self.session_id
        requests.post(
            self.url,
            headers=notify_headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            timeout=self.timeout,
        )
        return body

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        if not self.session_id:
            self.initialize()
        self._id += 1
        headers = {
            **self.headers,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        payload = {
            "jsonrpc": "2.0",
            "id": self._id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        }
        resp = requests.post(self.url, headers=headers, json=payload, timeout=self.timeout)
        data = self._parse(resp.text)
        if not data:
            raise RuntimeError(f"empty MCP response for {name}")
        if data.get("error"):
            raise RuntimeError(f"MCP {name}: {data['error']}")
        result = data.get("result") or {}
        content = result.get("content")
        if isinstance(content, list) and content:
            text = content[0].get("text")
            if text:
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return text
        return result


def retry(fn, attempts: int = 4, base: float = 1.5):
    last = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(base ** i)
    raise last


def clean_drive_id(raw: str) -> str:
    return re.sub(r"-\d+(?:-\d+)?$", "", raw)


def extract_drive_ids(html: str) -> list[str]:
    found = re.findall(r"ssk='[^']*:([A-Za-z0-9_-]{20,})'", html)
    cleaned = []
    for item in found:
        cid = clean_drive_id(item)
        if cid not in cleaned:
            cleaned.append(cid)
    if not cleaned:
        for item in re.findall(r'data-id="([A-Za-z0-9_-]{20,})"', html):
            if item not in cleaned:
                cleaned.append(item)
    return cleaned
