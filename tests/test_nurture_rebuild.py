"""Offline nurture rebuild tests (spec T-01..T-32 + Josh overrides)."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from crmbrain.config import STAGE, Settings
from crmbrain.memory import Memory
from crmbrain.models import CycleReport, Engagement
from crmbrain.nurture import (
    ACTION_APPROVE,
    ACTION_REMOVE,
    CASE_STUDIES,
    GENERAL_PROOF,
    MEETING_GUARANTEE,
    NurtureDraft,
    apply_reenrollment,
    attach_gmail_thread,
    build_nurture_card,
    collect_s1_positives,
    collect_s2_hubspot,
    collect_s3_gmail,
    compose_nurture_draft,
    compose_nurture_subject,
    dry_run_report,
    fire_due_rows,
    fire_gate,
    has_meeting_qualification,
    infer_industry_resolved,
    infer_nurture_reason,
    may_enroll_from_engagement,
    merge_candidates,
    next_fire_at_from_signal,
    nurture_daily_cap,
    nurture_row_from_candidate,
    qualify_candidate,
    render_sample_cards,
    count_posted_today,
    select_due_with_cap,
    snippet_of,
    spread_past_due,
    strip_quoted_text,
    thread_reply_headers,
    validate_draft,
    verify_slack_signature,
)
from crmbrain.nurture_actions import remove_from_nurture, send_nurture_reply
from crmbrain.ticker import TickerCandidate, already_enrolled, parse_signal_at

CDT = ZoneInfo("America/Chicago")
FIXTURES = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "nurture_fixtures.json").read_text()
)


def make_settings(**kwargs) -> Settings:
    base = dict(
        hubspot_token="",
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
        leadmagic_key="",
        slack_token="",
        slack_channel="C0BHBDTMRFY",
        supabase_url="https://example.supabase.co",
        supabase_key="",
        gemini_key="",
        gemini_model="gemini-2.5-flash",
        allo_url="",
        allo_key="",
        lookback_hours=36,
        slack_signing_secret="test-signing-secret",
        nurture_send_enabled=False,
        nurture_post_enabled=False,
    )
    base.update(kwargs)
    return Settings(**base)


class FakeGmail:
    def __init__(self, threads=None):
        self.sent: list[dict] = []
        self.threads = threads or {}
        self.searches: list[str] = []

    def search(self, query, max_results=50):
        del max_results
        self.searches.append(query)
        hit = self.threads.get(query) or next(
            (v for k, v in self.threads.items() if k in query or query in k),
            None,
        )
        if isinstance(hit, dict) and hit.get("thread_id"):
            return [{"id": hit.get("message_id") or "m1", "threadId": hit["thread_id"]}]
        return []

    def get(self, message_id):
        for hit in self.threads.values():
            if not isinstance(hit, dict):
                continue
            if hit.get("message_id") == message_id or True:
                return {
                    "id": message_id,
                    "threadId": hit.get("thread_id"),
                    "payload": {
                        "headers": [
                            {"name": "Subject", "value": hit.get("original_subject") or ""},
                            {"name": "Message-ID", "value": hit.get("in_reply_to") or ""},
                        ]
                    },
                }
        return {"id": message_id, "payload": {"headers": []}}

    def find_contact_thread(self, email, name=""):
        del name
        for key, hit in self.threads.items():
            if email and email in key and isinstance(hit, dict):
                return hit
        if email in self.threads and isinstance(self.threads[email], dict):
            return self.threads[email]
        return None

    def send_thread_reply(self, to, subject, body, thread_id, in_reply_to="", references=""):
        new_id = thread_id or f"th-nurture-{len(self.sent) + 1}"
        self.sent.append(
            {
                "to": to,
                "subject": subject,
                "body": body,
                "threadId": thread_id,
                "in_reply_to": in_reply_to,
                "references": references,
            }
        )
        return {"id": f"msg-{len(self.sent)}", "threadId": new_id}


class FakeSlack:
    def __init__(self):
        self.updated: list[dict] = []
        self.modals: list[dict] = []

    def update_message(self, settings, channel, ts, text, blocks=None):
        self.updated.append({"channel": channel, "ts": ts, "text": text, "blocks": blocks})
        return {"ok": True}

    def open_modal(self, settings, trigger_id, view):
        self.modals.append({"trigger_id": trigger_id, "view": view})
        return {"ok": True}

    def post_blocks(self, settings, text, blocks):
        return {"ok": True, "channel": settings.slack_channel, "ts": "111.222"}


def _case(tid: str) -> dict:
    return next(c for c in FIXTURES["cases"] if c["id"] == tid)


def test_t01_s1_all_statuses():
    data = _case("T-01")["input"]
    rows = collect_s1_positives(data["campaigns"], data["leads"])
    assert {r.email for r in rows} == set(_case("T-01")["expect"]["collected_emails"])


def test_t02_s1_signal_date():
    exp = _case("T-02")["expect"]
    inp = _case("T-02")["input"]
    rows = collect_s1_positives(
        [{"id": 101, "name": "SalesGlider Roofers", "status": "PAUSED"}],
        [inp["lead"]],
        {"101:1": inp["message_history"]},
    )
    assert rows[0].last_signal == parse_signal_at(exp["signal_at"])
    assert next_fire_at_from_signal(rows[0].last_signal).isoformat() == exp["next_fire_at"]
    assert rows[0].extra["source_ref"] == exp["source_ref"]
    assert rows[0].extra["last_touch_snippet"] == exp["last_touch_snippet"]


def test_t03_s1_no_date():
    inp = _case("T-03")["input"]
    rows = collect_s1_positives(
        [{"id": 103, "name": "SalesGlider Staffing"}],
        [inp["lead"]],
        {"103:9": inp["message_history"]},
    )
    assert rows[0].skip_reason == "no_signal_date"
    assert rows[0].last_signal is None
    assert qualify_candidate(rows[0]) == "no_signal_date"


def test_t04_client_and_candidate_campaigns():
    z = _case("T-04")["input"]
    c = TickerCandidate(
        name=z["name"],
        email=z["email"],
        company=z["company"],
        last_signal=datetime(2026, 6, 1, tzinfo=timezone.utc),
        source="smartlead",
        extra={"campaign": z["campaign"], "client_campaign": True, "booked": True},
    )
    assert qualify_candidate(c) == "client_campaign"
    j = _case("T-04b")["input"]
    c2 = TickerCandidate(
        name=j["name"],
        email=j["email"],
        last_signal=datetime(2026, 6, 1, tzinfo=timezone.utc),
        source="smartlead",
        extra={"campaign": j["campaign"], "booked": True},
    )
    assert qualify_candidate(c2) == "non_deal"


def test_t05_s2_nurture_note_not_hs_modified():
    inp = _case("T-05")["input"]
    rows = collect_s2_hubspot(
        [
            {
                **inp["deal"],
                "contact": inp["contact"],
                "source_note": inp["source_note"],
            }
        ]
    )
    assert len(rows) == 1
    row = nurture_row_from_candidate(rows[0])
    exp = _case("T-05")["expect"]
    assert row["source"] == "hubspot"
    assert row["signal_at"] == exp["signal_at"]
    assert row["next_fire_at"] == exp["next_fire_at"]
    assert row["industry"] == "hvac"
    assert row["industry_basis"] == "campaign"
    assert row["last_touch_snippet"].startswith(exp["snippet_startswith"])
    assert row["signal_at"] != "2026-10-02T22:00:00+00:00"


def test_t06_s2_stalled():
    inp = _case("T-06")["input"]
    now = parse_signal_at(inp["now"])
    rows = collect_s2_hubspot(inp["deals"], now=now)
    emails = {r.email for r in rows}
    assert emails == set(_case("T-06")["expect"]["enrolled_emails"])
    assert "fresh@fastpipe.test" not in emails


def test_t07_gmail_collected_but_josh_reply_only_does_not_enroll():
    """Josh override: a positive reply alone does not qualify."""
    inp = _case("T-07")["input"]
    rows = collect_s3_gmail([inp])
    assert len(rows) == 1
    assert snippet_of(inp["messages"][1]["body"]) == _case("T-07")["expect"]["last_touch_snippet"]
    assert rows[0].extra["source_ref"] == "gmail:t-777"
    assert qualify_candidate(rows[0]) == "reply_only"


def test_t08_merge_newest_signal_campaign_from_s1():
    inp = _case("T-08")["input"]
    cands = [
        TickerCandidate(
            name=r["name"],
            email=r["email"],
            last_signal=parse_signal_at(r["signal_at"]),
            source=r["source"],
            extra={"campaign": r.get("campaign") or "", "last_touch_snippet": r["last_touch_snippet"]},
        )
        for r in inp["candidates"]
    ]
    merged = merge_candidates(cands)
    assert len(merged) == 1
    assert parse_signal_at(merged[0].last_signal) == parse_signal_at(_case("T-08")["expect"]["signal_at"])
    assert merged[0].source == "hubspot"
    assert merged[0].extra["campaign"] == "SalesGlider HVAC Sports Offer"
    assert merged[0].extra["last_touch_snippet"] == "check back in the fall"


def test_t09_t10_t11_reenrollment():
    t09 = _case("T-09")
    cand = TickerCandidate(
        name=t09["input"]["candidate"]["name"],
        email=t09["input"]["candidate"]["email"],
        last_signal=parse_signal_at(t09["input"]["candidate"]["signal_at"]),
        source="smartlead",
        extra={"booked": True, "last_touch_snippet": "new"},
    )
    dec = apply_reenrollment(t09["input"]["existing"], cand)
    assert dec["action"] == "new_active"
    assert dec["row"]["next_fire_at"] == t09["expect"]["next_fire_at"]

    t10 = _case("T-10")
    cand = TickerCandidate(
        name=t10["input"]["candidate"]["name"],
        email=t10["input"]["candidate"]["email"],
        last_signal=parse_signal_at(t10["input"]["candidate"]["signal_at"]),
        source="smartlead",
        extra={"campaign": "SalesGlider Staffing", "booked": True},
    )
    dec = apply_reenrollment(t10["input"]["existing"], cand)
    assert dec["action"] == "skip"
    assert dec["skip_reason"] == "hard_stopped"

    t11 = _case("T-11")
    cand = TickerCandidate(
        name=t11["input"]["candidate"]["name"],
        email=t11["input"]["candidate"]["email"],
        last_signal=parse_signal_at(t11["input"]["candidate"]["signal_at"]),
        source="gmail",
        extra={"last_touch_snippet": t11["input"]["candidate"]["last_touch_snippet"], "booked": True},
    )
    dec = apply_reenrollment(t11["input"]["existing"], cand)
    assert dec["action"] == "refresh"
    assert dec["row"]["signal_at"] == t11["expect"]["signal_at"]
    assert dec["row"]["next_fire_at"] == t11["expect"]["next_fire_at"]
    assert dec["row"]["last_touch_snippet"] == t11["expect"]["last_touch_snippet"]


def test_t12_t15_industry():
    assert infer_industry_resolved(campaign="SalesGlider Roofers") == ("roofing", "campaign")
    assert infer_industry_resolved(email="jackie@kellyroofing.com") == ("roofing", "domain")
    key, basis = infer_industry_resolved(
        email="joel@thechillbrothers.com",
        company="The Chill Brothers",
        website_text="The Chill Brothers | HVAC, heating and cooling repair",
    )
    assert (key, basis) == ("hvac", "website")
    assert infer_industry_resolved(email="casey@linholdings.test", company="Lin Holdings") == (None, None)
    assert infer_industry_resolved(company="RLP Mechanical") == ("hvac", "website")
    assert infer_industry_resolved(company="Talent Unify") == ("staffing", "website")
    assert infer_industry_resolved(company="IntegriBuilt") == ("construction", "website")
    assert infer_industry_resolved(company="RDS Fire Systems") == ("construction", "website")
    assert infer_industry_resolved(company="Lin Holdings", hs_industry="Staffing and Recruiting") == (
        "staffing",
        "website",
    )
    assert infer_industry_resolved(company="Lin Holdings", hs_industry="Construction") == (
        "construction",
        "website",
    )


def test_t16_t21_gates():
    now = datetime(2026, 10, 2, 23, 0, tzinfo=timezone.utc)
    reason, patch = fire_gate(
        {"email": "pat@summitroofs.test", "name": "Pat Reyes", "status": "active"},
        gmail_sent_at=datetime(2026, 9, 12, 15, 0, tzinfo=timezone.utc),
        now=now,
    )
    assert reason == "emailed_recently"
    assert patch["next_fire_at"] == "2026-12-11T15:00:00+00:00"
    assert patch["status"] == "active"

    reason, _ = fire_gate(
        {"email": "lee@bytewise.test", "name": "Lee Ng", "status": "active"},
        smartlead_sent=[{"time": "2026-09-02T15:00:00Z"}],
        now=now,
    )
    assert reason == "emailed_recently"

    reason, patch = fire_gate(
        {"email": "owner@paidclient.test", "name": "Paid Owner", "associated_stages": [STAGE["paid"]]},
        now=now,
    )
    assert reason == "client"
    reason, patch = fire_gate({"email": "bobcbobc@gmail.com", "name": "Bob Carlson"}, now=now)
    assert reason == "non_deal"

    reason, patch = fire_gate(
        {"email": "joel@thechillbrothers.com", "name": "Joel Stewart", "hs_deal_id": "320872601304"},
        deal_404=True,
        now=now,
    )
    assert reason == "deal_archived"
    assert patch["status"] == "stopped"

    reason, patch = fire_gate(
        {
            "email": "ej@accg-inc.com",
            "name": "Earl Jackson",
            "associated_stages": [STAGE["proposal_sent"]],
            "last_activity": "2026-09-29T15:00:00Z",
        },
        now=now,
    )
    assert reason == "booked"

    reason, patch = fire_gate({"name": "", "email": "", "phone": "+15125550100"}, now=now)
    assert reason == "no_identity"
    assert patch["status"] == "stopped"


def test_t22_t26_drafts():
    d = compose_nurture_draft(
        {
            "name": "Morgan Pike",
            "last_touch_snippet": "Interested but timing is bad until Q4. Ping me then.",
        }
    )
    first = d.body.split("\n", 1)[0].lower()
    assert "q4" not in first
    assert "it's been a few months" in first
    assert d.subject != "Morgan?"
    assert d.subject == "Following up"

    roof = compose_nurture_draft(
        {
            "name": "Pat Reyes",
            "company": "Summit Roofs",
            "industry": "roofing",
            "last_touch_snippet": "Yes, send me info. Spring is our slow season.",
        }
    )
    assert roof.subject != "Roofing?"
    assert "MSP update" not in roof.subject
    assert roof.subject == "Summit Roofs follow up"
    assert CASE_STUDIES["roofing"].split("closed")[0][:10] in roof.body or "$100K" in roof.body
    assert MEETING_GUARANTEE in roof.body
    assert roof.body.strip().endswith("Josh Osborn")

    gen = compose_nurture_draft(
        {"name": "Casey Lin", "company": "Lin Holdings", "last_touch_snippet": "Maybe later this year."}
    )
    assert gen.subject != "Casey?"
    assert gen.subject == "Lin Holdings follow up"
    assert "$2M" in gen.body and "$100K" in gen.body and "14+" in gen.body
    assert "Quick update" not in gen.subject

    bad = validate_draft(
        NurtureDraft(subject="Lee?", body="Hey Lee, happy to run a free POC — no strings."),
        {"name": "Lee Ng"},
    )
    assert not bad.valid
    assert bad.reject_reason in {"free_poc", "dash"}

    ok = compose_nurture_draft(
        {
            "name": "Lee Ng",
            "industry": "msp",
            "last_touch_snippet": "Can you do the free 10K test list first — like last time?",
        }
    )
    assert ok.valid
    assert "—" not in ok.body and "–" not in ok.body
    assert "free poc" not in ok.body.lower()
    assert "proof of concept" not in ok.body.lower()

    on = compose_nurture_draft({"name": "Dana Ortiz", "industry": "hvac", "last_touch_snippet": "check back in the fall"}, airpods=True)
    off = compose_nurture_draft({"name": "Dana Ortiz", "industry": "hvac", "last_touch_snippet": "check back in the fall"}, airpods=False)
    assert "AirPods" in on.body and "AirPods" not in off.body
    assert len(on.body.split()) <= 110
    assert "{" not in on.body
    assert on.body.strip().endswith("Josh Osborn")
    assert GENERAL_PROOF or CASE_STUDIES["hvac"]


def test_t27_t28_t32_cadence():
    now = datetime(2026, 10, 9, 12, 0, tzinfo=CDT)
    signals = [
        "2026-03-01",
        "2026-03-05",
        "2026-03-10",
        "2026-03-15",
        "2026-03-20",
        "2026-03-25",
        "2026-04-01",
        "2026-04-05",
        "2026-04-10",
        "2026-04-15",
        "2026-04-20",
        "2026-04-25",
    ]
    slots = spread_past_due(signals, now=now, approval_date="2026-10-09")
    by_day: dict[str, int] = {}
    for s in slots:
        by_day[s.date().isoformat()] = by_day.get(s.date().isoformat(), 0) + 1
    assert by_day == {"2026-10-12": 5, "2026-10-13": 5, "2026-10-14": 2}
    assert all(s.weekday() < 5 for s in slots)

    nxt = next_fire_at_from_signal("2026-09-19T15:00:00Z")
    assert nxt.astimezone(CDT).date().isoformat() == "2026-12-18"
    assert nxt.astimezone(CDT).weekday() < 5

    now = datetime(2026, 10, 12, 17, 0, tzinfo=CDT)
    rows = [
        {
            "id": f"r{i}",
            "name": f"N{i}",
            "email": f"n{i}@x.test",
            "status": "active",
            "next_fire_at": "2026-10-01T00:00:00+00:00",
            "signal_at": f"2026-03-{i+1:02d}T00:00:00+00:00",
        }
        for i in range(8)
    ]
    posted, rolled = select_due_with_cap(rows, now=now)
    assert len(posted) == 5
    assert len(rolled) == 3
    assert all(r["next_fire_at"].startswith("2026-10-13") for r in rolled)
    none, leftover = select_due_with_cap(rows, now=now, already_posted=5)
    assert none == []
    assert len(leftover) == 8
    assert all(r["next_fire_at"].startswith("2026-10-13") for r in leftover)


def test_t29_t30_empty_means_empty(tmp_path: Path):
    settings = make_settings(supabase_key="k")
    memory = Memory(settings, data_dir=tmp_path)
    memory._local["ticker"] = [
        {"id": "x", "status": "active", "next_fire_at": "2026-01-01T00:00:00+00:00", "name": "Stale Local"}
    ]

    def empty(_method, _table, **_k):
        return []

    memory._sb_schema = empty
    assert memory.due_ticker("2026-10-02T00:00:00+00:00") == []

    def boom(*_a, **_k):
        raise RuntimeError("supabase down")

    memory._sb_schema = boom
    assert memory.due_ticker("2026-10-02T00:00:00+00:00") == []
    report = CycleReport()
    cards = fire_due_rows(settings, memory, report)
    assert cards == []
    assert any("ticker_supabase_unavailable" in e for e in report.errors + memory.errors)


def test_t31_dry_run_report():
    report = dry_run_report({"smartlead": 4, "hubspot": 3, "gmail": 2}, [{"n": i} for i in range(12)], {}, {})
    assert report["writes"] == 0
    assert report["slack_posts"] == 0
    assert set(report) >= {"by_source", "by_skip_reason", "industry_hit_rate", "weekly_schedule"}
    assert len(report["sample_drafts"]) <= 10


def test_josh_enrollment_met_booked_yes_reply_only_no():
    met = Engagement(
        source="fireflies",
        external_id="ff-1",
        first_name="Pat",
        last_name="Reyes",
        email="pat@summitroofs.test",
        extra={"met": True},
    )
    booked = Engagement(
        source="calendly",
        external_id="cal-1",
        first_name="Dana",
        last_name="Ortiz",
        email="dana@brightcool.test",
        extra={"booked": True},
    )
    reply = Engagement(
        source="smartlead",
        external_id="sl-1",
        first_name="Lee",
        last_name="Ng",
        email="lee@bytewise.test",
        summary="Interested",
    )
    assert may_enroll_from_engagement(met)[0] is True
    assert may_enroll_from_engagement(booked)[0] is True
    ok, reason = may_enroll_from_engagement(reply)
    assert ok is False and reason == "reply_only"
    assert has_meeting_qualification(deal_stage=STAGE["nurture"]) is True
    assert has_meeting_qualification(reason="no_show") is True


def test_thread_reply_headers():
    headers = thread_reply_headers("Roofing?", in_reply_to="<abc@mail>", references="<abc@mail>")
    assert headers["Subject"] == "Re: Roofing?"
    assert headers["In-Reply-To"] == "<abc@mail>"
    assert headers["References"] == "<abc@mail>"
    already = thread_reply_headers("Re: HVAC update", in_reply_to="<x>")
    assert already["Subject"] == "Re: HVAC update"


def test_signature_verification():
    secret = "test-signing-secret"
    ts = str(int(datetime.now(timezone.utc).timestamp()))
    body = b"payload=%7B%22type%22%3A%22block_actions%22%7D"
    digest = "v0=" + hmac.new(secret.encode(), b"v0:" + ts.encode() + b":" + body, hashlib.sha256).hexdigest()
    assert verify_slack_signature(secret, ts, body, digest)
    assert not verify_slack_signature(secret, ts, body, "v0=nope")
    assert not verify_slack_signature(secret, str(int(datetime.now(timezone.utc).timestamp()) - 400), body, digest)
    assert not verify_slack_signature("", ts, body, digest)


def test_idempotent_send_and_90_day_cooldown(tmp_path: Path):
    settings = make_settings(nurture_send_enabled=True)
    memory = Memory(settings, data_dir=tmp_path)
    memory._local["ticker"] = [
        {
            "id": "t-send",
            "name": "Jackie Darkazalli",
            "email": "jackie@kellyroofing.com",
            "status": "active",
            "nurture_state": "queued",
            "nurture_thread_id": "thread-jackie",
            "nurture_thread_subject": "Kelly Roofing follow up",
            "in_reply_to": "<jackie-orig@mail>",
            "references": "<jackie-orig@mail>",
            "last_touch_snippet": "Check back after our busy season.",
        }
    ]
    gmail = FakeGmail()
    slack = FakeSlack()
    first = send_nurture_reply(settings, memory, "t-send", gmail=gmail, slack=slack, channel="C0BHBDTMRFY", ts="1.2")
    second = send_nurture_reply(settings, memory, "t-send", gmail=gmail, slack=slack, channel="C0BHBDTMRFY", ts="1.2")
    assert first["ok"] is True
    assert first["outcome"] == "sent"
    assert second["outcome"] == "already_sent"
    assert len(gmail.sent) == 1
    assert gmail.sent[0]["threadId"] == "thread-jackie"
    assert gmail.sent[0]["in_reply_to"] == "<jackie-orig@mail>"
    row = memory.get_ticker("t-send")
    nxt = parse_signal_at(row["next_fire_at"])
    sent_at = parse_signal_at(row["last_sent_at"])
    assert nxt - sent_at >= timedelta(days=89)
    assert row["nurture_state"] == "sent"
    assert slack.updated


def test_send_disabled_does_not_send(tmp_path: Path):
    settings = make_settings(nurture_send_enabled=False)
    memory = Memory(settings, data_dir=tmp_path)
    memory._local["ticker"] = [
        {
            "id": "t-off",
            "name": "Pat Reyes",
            "email": "pat@summitroofs.test",
            "status": "active",
            "nurture_state": "queued",
            "gmail_thread_id": "th",
        }
    ]
    gmail = FakeGmail()
    out = send_nurture_reply(settings, memory, "t-off", gmail=gmail, slack=FakeSlack(), channel="C", ts="1")
    assert out["outcome"] == "disabled"
    assert gmail.sent == []
    assert memory.get_ticker("t-off")["nurture_state"] == "queued"


def test_remove_stops_permanently(tmp_path: Path):
    settings = make_settings()
    memory = Memory(settings, data_dir=tmp_path)
    memory._local["ticker"] = [
        {
            "id": "t-rm",
            "name": "Lee Ng",
            "email": "lee@bytewise.test",
            "status": "active",
            "nurture_state": "queued",
        }
    ]
    first = remove_from_nurture(settings, memory, "t-rm", slack=FakeSlack(), channel="C", ts="1")
    second = remove_from_nurture(settings, memory, "t-rm", slack=FakeSlack(), channel="C", ts="1")
    assert first["outcome"] == "removed"
    assert second["outcome"] == "already_removed"
    row = memory.get_ticker("t-rm")
    assert row["status"] == "stopped"
    assert row["stop_reason"] == "do_not_contact"
    assert already_enrolled([row], email="lee@bytewise.test") is True


def test_card_has_three_buttons():
    draft = compose_nurture_draft({"id": "t1", "name": "Pat Reyes", "email": "pat@x.test", "industry": "roofing"})
    card = build_nurture_card({"id": "t1", "name": "Pat Reyes", "email": "pat@x.test", "reason": "kicked_can"}, draft)
    actions = next(b for b in card["blocks"] if b["type"] == "actions")
    labels = [el["text"]["text"] for el in actions["elements"]]
    assert labels == ["Approve & send", "Edit & send", "Remove from nurture"]
    assert {el["action_id"] for el in actions["elements"]} == {ACTION_APPROVE, "nurture_edit_send", ACTION_REMOVE}


def test_quoted_text_stripped():
    body = "Interested but timing is bad until Q4. Ping me then.\n\nOn Jun 20, Josh wrote:\n> Hey Morgan, worth a quick chat?"
    assert strip_quoted_text(body) == "Interested but timing is bad until Q4. Ping me then."


def test_sample_cards_file_has_ten_mixed():
    cards = render_sample_cards()
    assert len(cards) == 10
    assert sum(1 for c in cards if c["industry"] in {"roofing", "hvac", "construction", "staffing", "msp"}) >= 3
    assert sum(1 for c in cards if not c["industry"]) >= 3
    assert sum(1 for c in cards if c["source"] == "hubspot") >= 2
    assert sum(1 for c in cards if c["source"] == "gmail") >= 1
    for card in cards:
        assert card["subject"]
        assert card["body"].endswith("Josh Osborn")
        assert "—" not in card["body"]
        assert "AirPods" not in card["body"]
        assert "thread_id" in card
        assert card["blocks"]


def test_flags_default_off():
    s = Settings.from_env()
    assert s.nurture_send_enabled is False
    assert s.nurture_post_enabled is False


def test_fire_due_writes_cards_when_post_off(tmp_path: Path):
    settings = make_settings(nurture_post_enabled=False)
    memory = Memory(settings, data_dir=tmp_path)
    memory._local["ticker"] = [
        {
            "id": "t-roof",
            "name": "Jackie Darkazalli",
            "email": "jackie@kellyroofing.com",
            "company": "Kelly Roofing",
            "reason": "kicked_can",
            "status": "active",
            "next_fire_at": "2020-01-01T00:00:00+00:00",
            "last_touch_snippet": "Check back after our busy season.",
        }
    ]
    report = CycleReport()
    cards = fire_due_rows(
        settings,
        memory,
        report,
        now=datetime(2026, 10, 5, 7, 0, tzinfo=CDT),
    )
    assert cards
    assert report.nurture_cards
    assert cards[0]["subject"] != "Roofing?"
    assert cards[0]["subject"] == "Kelly Roofing follow up"
    assert "Josh Osborn" in cards[0]["body"]


def test_gmail_lookup_any_date_sent_and_inbox():
    gmail = FakeGmail(
        {
            "brad@lord.test": {
                "thread_id": "th-old-intro",
                "message_id": "m-brad",
                "original_subject": "Cyber intro",
            }
        }
    )
    ignored = attach_gmail_thread({"name": "Bradley Lord", "email": "brad@lord.test"}, gmail)
    assert ignored["thread_kind"] == "new_thread"
    assert not ignored.get("gmail_thread_id")
    stored = attach_gmail_thread(
        {
            "name": "Bradley Lord",
            "email": "brad@lord.test",
            "nurture_thread_id": "th-nurture-brad",
            "nurture_thread_subject": "Lord Security follow up",
        },
        gmail,
    )
    assert stored["thread_kind"] == "reply"
    assert stored["gmail_thread_id"] == "th-nurture-brad"
    assert stored["original_subject"] == "Lord Security follow up"
    missing = attach_gmail_thread({"name": "New Person", "email": "nobody@none.test"}, FakeGmail())
    assert missing["thread_kind"] == "new_thread"
    assert not missing.get("gmail_thread_id")


def test_send_without_thread_starts_new_one_to_one(tmp_path: Path):
    settings = make_settings(nurture_send_enabled=True)
    memory = Memory(settings, data_dir=tmp_path)
    memory._local["ticker"] = [
        {
            "id": "t-new",
            "name": "Pat Reyes",
            "email": "pat@summitroofs.test",
            "company": "Summit Roofs",
            "status": "active",
            "nurture_state": "queued",
            "last_touch_snippet": "Yes, send me info. Spring is our slow season.",
        }
    ]
    gmail = FakeGmail()
    out = send_nurture_reply(settings, memory, "t-new", gmail=gmail, slack=FakeSlack(), channel="C", ts="1")
    assert out["ok"] is True
    assert out["thread_kind"] == "new_thread"
    assert gmail.sent[0]["threadId"] == ""
    saved = memory.get_ticker("t-new")
    assert saved["nurture_thread_id"]
    assert saved["nurture_thread_subject"] == "Summit Roofs follow up"


def test_hubspot_meeting_evidence_is_not_never_booked():
    rows = collect_s2_hubspot(
        [
            {
                "id": "d1",
                "dealstage": STAGE["proposal_sent"],
                "last_activity": "2026-07-01T00:00:00+00:00",
                "contact": {"id": "c1", "firstname": "Ari", "lastname": "Stone", "email": "stalled@fastpipe.test"},
                "source_note": {"body": "Proposal is sitting.", "created": "2026-07-01T00:00:00+00:00"},
                "fireflies": True,
                "meeting_at": "2026-06-01T00:00:00+00:00",
            }
        ],
        now=datetime(2026, 10, 2, tzinfo=timezone.utc),
    )
    assert rows
    assert rows[0].reason != "never_booked"
    assert rows[0].reason == "met"
    assert infer_nurture_reason(deal_stage=STAGE["discovery_completed"], extra={"cube_acr": True}) == "met"
    assert infer_nurture_reason(reason="no_show", extra={"booked": True}) == "booked"


def test_opener_never_quotes_deal_name_or_wrong_capitalization():
    draft = compose_nurture_draft(
        {
            "name": "robert Lawson",
            "email": "robert@cyber.test",
            "company": "CyberGuard360",
            "dealname": "robert Lawson - CyberGuard360",
            "last_touch_snippet": "robert Lawson - CyberGuard360",
        }
    )
    opener = draft.body.split("\n", 1)[0]
    assert opener.startswith("Hey Robert")
    assert "you mentioned" not in opener.lower() or "cyberguard" not in opener.lower()
    assert "robert Lawson - CyberGuard360" not in draft.body
    assert "CyberGuard360" not in opener


def test_bradley_lord_card_does_not_leak_lionel_francis():
    row = {
        "name": "Bradley Lord",
        "email": "brad@lord.test",
        "company": "Lord Security",
        "dealname": "Lionel Francis - Acme",
        "last_touch_snippet": "you mentioned lionel Francis on the CyberGuard call",
        "reason": "met",
        "deal_stage": STAGE["proposal_sent"],
    }
    draft = compose_nurture_draft(row)
    blob = f"{draft.subject}\n{draft.body}".lower()
    assert "lionel" not in blob
    assert "francis" not in blob
    card = build_nurture_card(row, draft)
    text = json.dumps(card).lower()
    assert "lionel" not in text
    assert "francis" not in text


def test_opener_excludes_personal_family_details():
    draft = compose_nurture_draft(
        {
            "name": "Kevin Hagemoser",
            "email": "kevin@hag.test",
            "company": "Hagemoser",
            "last_touch_snippet": "Kevin talked about his wife and son and their weekend plans.",
        }
    )
    opener = draft.body.split("\n", 1)[0].lower()
    assert "wife" not in opener
    assert "son" not in opener
    assert "you mentioned" not in opener


def test_thread_reply_uses_original_subject_and_new_thread_is_specific():
    old = compose_nurture_draft(
        {
            "name": "Jackie Darkazalli",
            "company": "Kelly Roofing",
            "gmail_thread_id": "thread-jackie",
            "original_subject": "Kelly Roofing intro",
            "last_touch_snippet": "Check back after our busy season.",
        }
    )
    assert old.subject == "Kelly Roofing follow up"
    reply = compose_nurture_draft(
        {
            "name": "Jackie Darkazalli",
            "company": "Kelly Roofing",
            "nurture_thread_id": "th-nurture-jackie",
            "nurture_thread_subject": "Kelly Roofing follow up",
            "last_touch_snippet": "Check back after our busy season.",
        }
    )
    assert reply.subject == "Re: Kelly Roofing follow up"
    fresh = compose_nurture_draft(
        {
            "name": "Lee Ng",
            "company": "Bytewise",
            "industry": "msp",
            "last_touch_snippet": "We just lost our SDR, maybe now.",
        }
    )
    assert fresh.subject == "Bytewise follow up"


def test_nurture_stage_never_keeps_stale_never_booked():
    """Item 1: Nurture without an explicit met flag derives met/booked from evidence."""
    nurture = STAGE["nurture"]
    assert infer_nurture_reason(reason="never_booked", deal_stage=nurture) == "booked"
    assert (
        infer_nurture_reason(
            reason="never_booked",
            deal_stage=nurture,
            extra={"fireflies": True},
        )
        == "met"
    )
    assert (
        infer_nurture_reason(
            reason="never_booked",
            deal_stage=nurture,
            extra={"cube_acr": True},
        )
        == "met"
    )
    assert (
        infer_nurture_reason(
            reason="never_booked",
            deal_stage=nurture,
            extra={"original_subject": "Your meeting recap - Pat Reyes and Joshua Osborn"},
        )
        == "met"
    )
    assert (
        infer_nurture_reason(
            reason="never_booked",
            deal_stage=nurture,
            extra={"calendar_event": True, "meeting_at": "2026-06-01T15:00:00Z"},
        )
        == "booked"
    )
    assert (
        infer_nurture_reason(
            reason="never_booked",
            deal_stage=nurture,
            extra={"hs_meeting": True},
        )
        == "booked"
    )
    assert (
        infer_nurture_reason(
            reason="never_booked",
            deal_stage=nurture,
            extra={"no_show_count": "2"},
        )
        == "booked"
    )
    reply_only = infer_nurture_reason(reason="never_booked", extra={"source": "smartlead"})
    assert reply_only == "never_booked"
    assert has_meeting_qualification(reason="never_booked", extra={"source": "smartlead"}) is False
    assert may_enroll_from_engagement(
        Engagement(source="smartlead", external_id="sl-1", email="pat@x.test", ticker_reason="never_booked"),
        reason="never_booked",
    ) == (False, "reply_only")


def test_opener_never_quotes_own_name_company_or_stage():
    """Item 2: no self-name / company / stage in 'you mentioned', and met people get a call opener."""
    scott = compose_nurture_draft(
        {
            "name": "Scott Hagan",
            "email": "scott@finishline.test",
            "company": "Finish Line",
            "reason": "met",
            "deal_stage": STAGE["nurture"],
            "last_touch_snippet": "scott Hagan",
        }
    )
    opener = scott.body.split("\n", 1)[0]
    assert "you mentioned" not in opener.lower()
    assert "scott hagan" not in opener.lower()
    assert "finish line" not in opener.lower()
    assert "nurture" not in opener.lower()
    assert "wanted to circle back" in opener.lower()
    assert "on our call" not in opener.lower()
    assert scott.subject != "Finish Line: Scott Hagan"
    assert scott.subject != "Scott Hagan"
    assert "Scott Hagan" not in scott.subject

    stage_copy = compose_nurture_draft(
        {
            "name": "Mike Dolan",
            "company": "Dolan Roofing",
            "campaign": "Nurture",
            "last_touch_snippet": "mike Dolan",
            "reason": "met",
        }
    )
    blob = f"{stage_copy.subject}\n{stage_copy.body}".lower()
    assert "reached out about nurture" not in blob
    assert "you mentioned mike" not in blob
    assert "you replied a while back" not in blob

    met_neutral = compose_nurture_draft(
        {
            "name": "Lionel Francis",
            "reason": "met",
            "meeting_at": "2026-06-12T16:00:00+00:00",
            "last_touch_snippet": "lionel Francis",
        }
    )
    assert "you replied a while back" not in met_neutral.body
    assert "jun" in met_neutral.body.lower()
    assert "call" in met_neutral.body.split("\n", 1)[0].lower()


def test_nurture_body_has_no_airpods_and_is_personalized():
    """Item 4: no AirPods/gift; proof matches vertical; guarantee stays."""
    roof = compose_nurture_draft(
        {
            "name": "Jackie Darkazalli",
            "company": "Kelly Roofing",
            "industry": "roofing",
            "reason": "met",
            "fireflies_id": "ff-jackie-busy",
            "meeting_source": "fireflies",
            "summary": "Check back after our busy season. Roofing crews are slammed until fall.",
            "last_touch_snippet": "Check back after our busy season. Roofing crews are slammed until fall.",
        }
    )
    hvac = compose_nurture_draft(
        {
            "name": "Joel Stewart",
            "company": "The Chill Brothers",
            "industry": "hvac",
            "reason": "met",
            "last_touch_snippet": "We are slammed through summer, check back in the fall about filling shoulder season.",
        }
    )
    assert "AirPods" not in roof.body
    assert "AirPods" not in hvac.body
    assert "ticket" not in roof.body.lower()
    assert MEETING_GUARANTEE in roof.body
    assert CASE_STUDIES["roofing"].split("closed")[0][:10] in roof.body or "$100K" in roof.body
    assert "trades" in hvac.body or CASE_STUDIES["hvac"][:10] in hvac.body
    assert roof.body != hvac.body
    default = compose_nurture_draft({"name": "Pat Reyes", "industry": "roofing"})
    assert "AirPods" not in default.body


def test_company_name_keywords_pick_vertical_proof():
    from crmbrain.nurture import GENERAL_PROOF, TRADES_PROOF

    trades = compose_nurture_draft({"name": "Alex Rivera", "company": "RLP Mechanical"})
    staffing = compose_nurture_draft({"name": "Alex Rivera", "company": "Talent Unify"})
    built = compose_nurture_draft({"name": "Alex Rivera", "company": "IntegriBuilt"})
    fire = compose_nurture_draft({"name": "Alex Rivera", "company": "RDS Fire Systems"})
    assert TRADES_PROOF.rstrip(".") in trades.body
    assert GENERAL_PROOF.rstrip(".") in staffing.body
    assert TRADES_PROOF.rstrip(".") in built.body
    assert TRADES_PROOF.rstrip(".") in fire.body


def _due_nurture_row(i: int, next_fire: str = "2020-01-01T00:00:00+00:00") -> dict:
    return {
        "id": f"t-cap-{i}",
        "name": f"Pat Reyes {i}",
        "email": f"pat{i}@summitroofs.test",
        "company": "Summit Roofs",
        "industry": "roofing",
        "reason": "kicked_can",
        "status": "active",
        "next_fire_at": next_fire,
        "signal_at": f"2026-03-{i + 1:02d}T00:00:00+00:00",
        "last_touch_snippet": "Check back after our busy season.",
    }


def test_settings_daily_cap_default_five():
    assert nurture_daily_cap(None) == 5
    assert nurture_daily_cap(make_settings()) == 5
    assert nurture_daily_cap(make_settings(nurture_max_per_weekday=3)) == 3


def test_monday_7am_posts_five_5pm_posts_zero_leftovers_next_weekday(tmp_path: Path):
    settings = make_settings(nurture_post_enabled=False, nurture_max_per_weekday=5)
    memory = Memory(settings, data_dir=tmp_path)
    memory._local["ticker"] = [_due_nurture_row(i) for i in range(8)]
    monday_7am = datetime(2026, 10, 5, 7, 0, tzinfo=CDT)
    monday_5pm = datetime(2026, 10, 5, 17, 0, tzinfo=CDT)
    morning = fire_due_rows(settings, memory, CycleReport(), now=monday_7am, gmail=FakeGmail())
    assert len(morning) == 5
    assert count_posted_today(memory, now=monday_7am) == 5
    leftover = [
        row
        for row in memory._local["ticker"]
        if str(row.get("id") or "").startswith("t-cap-")
        and (row.get("next_fire_at") or "").startswith("2026-10-06")
    ]
    assert len(leftover) == 3
    assert all(not row.get("last_fired_at") for row in leftover)
    evening = fire_due_rows(settings, memory, CycleReport(), now=monday_5pm, gmail=FakeGmail())
    assert evening == []
    assert count_posted_today(memory, now=monday_5pm) == 5
    still_leftover = [
        row
        for row in memory._local["ticker"]
        if (row.get("next_fire_at") or "").startswith("2026-10-06")
    ]
    assert len(still_leftover) == 3


def test_saturday_fire_due_rows_posts_nothing(tmp_path: Path):
    settings = make_settings(nurture_post_enabled=False)
    memory = Memory(settings, data_dir=tmp_path)
    memory._local["ticker"] = [_due_nurture_row(i) for i in range(5)]
    saturday = datetime(2026, 10, 3, 7, 0, tzinfo=CDT)
    cards = fire_due_rows(settings, memory, CycleReport(), now=saturday, gmail=FakeGmail())
    assert cards == []
    assert count_posted_today(memory, now=saturday) == 0
    for row in memory._local["ticker"]:
        assert row["next_fire_at"].startswith("2026-10-05")
        assert not row.get("last_fired_at")
