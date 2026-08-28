from __future__ import annotations

import base64
from email.mime.text import MIMEText
from typing import Any

import requests

from crmbrain.config import Settings


class Gmail:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._token = ""

    def token(self) -> str:
        if self._token:
            return self._token
        resp = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": self.settings.gmail_client_id,
                "client_secret": self.settings.gmail_client_secret,
                "refresh_token": self.settings.gmail_refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=30,
        )
        resp.raise_for_status()
        self._token = resp.json()["access_token"]
        return self._token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token()}"}

    def search(self, query: str, max_results: int = 50) -> list[dict]:
        resp = requests.get(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages",
            headers=self._headers(),
            params={"q": query, "maxResults": max_results},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("messages", [])

    def get(self, message_id: str) -> dict[str, Any]:
        resp = requests.get(
            f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}",
            headers=self._headers(),
            params={"format": "full"},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def headers_map(self, message: dict) -> dict[str, str]:
        headers = {}
        for item in message.get("payload", {}).get("headers", []):
            headers[item["name"].lower()] = item.get("value", "")
        return headers

    def send(self, to: str, subject: str, body: str) -> None:
        msg = MIMEText(body)
        msg["to"] = to
        msg["from"] = "joshua@salesglidergrowth.com"
        msg["subject"] = subject
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        resp = requests.post(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            headers={**self._headers(), "Content-Type": "application/json"},
            json={"raw": raw},
            timeout=30,
        )
        resp.raise_for_status()
