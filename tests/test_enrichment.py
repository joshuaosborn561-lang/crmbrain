import importlib
from dataclasses import fields
from pathlib import Path

import pytest

from crmbrain.config import Settings
from crmbrain import enrichment as enrichment_mod
from crmbrain.enrichment import enrich, fill_linkedin
from crmbrain.identity import (
    looks_like_email,
    looks_like_phone,
    normalize_phone,
    should_skip_email,
    usable_linkedin,
)
from crmbrain.models import Engagement


def make_settings(**kwargs) -> Settings:
    base = dict(
        hubspot_token="tok",
        gmail_client_id="",
        gmail_client_secret="",
        gmail_refresh_token="",
        josh_brief_email="joshua@salesglidergrowth.com",
        fireflies_key="",
        smartlead_key="",
        cube_folder="folder",
        heyreach_url="https://mcp.heyreach.io/mcp",
        heyreach_key="",
        heyreach_campaign_id=1,
        heyreach_linkedin_account_id=1,
        enrichment_url="",
        enrichment_client_tag="salesglider",
        slack_token="",
        slack_channel="C0BHBDTMRFY",
        supabase_url="https://example.supabase.co",
        supabase_key="",
        gemini_key="",
        gemini_model="gemini-2.5-flash",
        allo_url="",
        allo_key="",
        lookback_hours=36,
    )
    base.update(kwargs)
    return Settings(**base)


def _incomplete_person() -> Engagement:
    return Engagement(
        source="test",
        external_id="1",
        first_name="Laura",
        last_name="Klein",
        company="GRN Plano",
        domain="grnplano.com",
    )


def test_settings_has_no_leadmagic_key():
    assert "leadmagic_key" not in {f.name for f in fields(Settings)}
    settings = make_settings()
    assert not hasattr(settings, "leadmagic_key")


def test_from_env_ignores_leadmagic_api_key(monkeypatch):
    monkeypatch.setenv("LEADMAGIC_API_KEY", "should-be-ignored")
    settings = Settings.from_env()
    assert not hasattr(settings, "leadmagic_key")


def test_leadmagic_module_is_gone():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("crmbrain.leadmagic")


def test_enrichment_source_has_no_leadmagic():
    text = Path(enrichment_mod.__file__).read_text(encoding="utf-8")
    assert "leadmagic" not in text.lower()
    assert "find_email" not in text
    assert "find_mobile" not in text


def test_enrich_leaves_gaps_when_waterfall_is_off():
    ev = enrich(make_settings(), _incomplete_person())
    assert ev.email == ""
    assert ev.phone == ""


def test_enrich_waterfall_failure_does_not_fill_gaps(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("waterfall down")

    monkeypatch.setattr("crmbrain.enrichment._enrich_waterfall", boom)
    ev = enrich(make_settings(enrichment_url="https://example.invalid/mcp"), _incomplete_person())
    assert ev.email == ""
    assert ev.phone == ""


def test_enrich_never_posts_to_leadmagic(monkeypatch):
    posted: list[str] = []

    def fake_post(url, *a, **k):
        posted.append(str(url))
        raise AssertionError(f"unexpected POST {url}")

    monkeypatch.setattr("requests.post", fake_post)
    enrich(make_settings(), _incomplete_person())
    assert posted == []
    assert not any("leadmagic" in url.lower() for url in posted)


def test_fill_linkedin_uses_waterfall_only(monkeypatch):
    calls = {"n": 0}

    def fake_waterfall(settings, ev, domain):
        calls["n"] += 1
        ev.linkedin_url = "https://www.linkedin.com/in/lauramklein"
        return ev

    monkeypatch.setattr("crmbrain.enrichment._enrich_waterfall", fake_waterfall)
    ev = fill_linkedin(
        make_settings(enrichment_url="https://example.test/mcp"),
        Engagement(source="test", external_id="1", email="lklein@grnplano.com"),
    )
    assert calls["n"] == 1
    assert ev.linkedin_url == "https://www.linkedin.com/in/lauramklein"


def test_identity_phone_and_email_helpers():
    assert normalize_phone("4697011712") == "+14697011712"
    assert normalize_phone(None) == ""
    assert looks_like_phone("4697011712")
    assert looks_like_phone("***-***-1712")
    assert not looks_like_phone("123")
    assert looks_like_email("lklein@grnplano.com")
    assert not looks_like_email("not-an-email")
    assert should_skip_email("joshua@salesglidergrowth.com")
    assert should_skip_email("fred@fireflies.ai")
    assert not should_skip_email("lklein@grnplano.com")
    assert usable_linkedin("in/lauramklein") == "https://www.linkedin.com/in/lauramklein"
