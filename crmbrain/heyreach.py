from __future__ import annotations

import time

from crmbrain.config import Settings
from crmbrain.http_mcp import McpClient
from crmbrain.models import Engagement


class HeyReach:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = McpClient(settings.heyreach_url, headers={"x-mcp-key": settings.heyreach_key})

    def campaign(self) -> dict:
        return self.client.call("get_campaign", {"campaignId": self.settings.heyreach_campaign_id})

    def ensure_running(self) -> None:
        data = self.campaign()
        status = ""
        if isinstance(data, dict):
            status = str(data.get("status") or data.get("campaignStatus") or "").upper()
            inner = data.get("campaign") if isinstance(data.get("campaign"), dict) else data
            status = str(inner.get("status") or inner.get("campaignStatus") or status).upper()
        if status in {"FINISHED", "PAUSED", "STOPPED", "COMPLETED"}:
            self.client.call("resume_campaign", {"campaignId": self.settings.heyreach_campaign_id})
            time.sleep(5)

    def add_lead(self, ev: Engagement) -> str:
        if not ev.linkedin_url:
            return "skipped:no-linkedin"
        self.ensure_running()
        first = ev.first_name or (ev.display_name().split(" ")[0] if ev.display_name() else "")
        last = ev.last_name or (" ".join(ev.display_name().split(" ")[1:]) if ev.display_name() else "")
        payload = {
            "campaignId": self.settings.heyreach_campaign_id,
            "accountLeadPairs": [
                {
                    "linkedInAccountId": self.settings.heyreach_linkedin_account_id,
                    "lead": {
                        "profileUrl": ev.linkedin_url,
                        "firstName": first,
                        "lastName": last,
                        "companyName": ev.company,
                    },
                }
            ],
        }
        self.client.call("add_leads_to_campaign_v2", payload)
        return "queued"

    def recent_conversations(self) -> list[Engagement]:
        """LinkedIn replies / accepts are engagement."""
        try:
            data = self.client.call(
                "get_conversations_v2",
                {
                    "accountIds": [self.settings.heyreach_linkedin_account_id],
                    "limit": 50,
                    "offset": 0,
                },
            )
        except Exception:
            return []
        rows = data if isinstance(data, list) else (data.get("items") or data.get("conversations") or data.get("data") or [])
        out: list[Engagement] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            correspondent = row.get("correspondent") or row.get("lead") or {}
            last = row.get("lastMessage") or row.get("last_message") or {}
            text = last.get("text") or last.get("body") or row.get("snippet") or ""
            profile = (
                correspondent.get("profileUrl")
                or correspondent.get("linkedinUrl")
                or row.get("profileUrl")
                or ""
            )
            first = correspondent.get("firstName") or ""
            lastn = correspondent.get("lastName") or ""
            company = correspondent.get("companyName") or ""
            if not (profile or first or text):
                continue
            out.append(
                Engagement(
                    source="heyreach",
                    external_id=str(row.get("id") or row.get("conversationId") or profile or f"{first}-{lastn}"),
                    first_name=first,
                    last_name=lastn,
                    company=company,
                    linkedin_url=profile,
                    summary=str(text)[:500],
                )
            )
        return out
