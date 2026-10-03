from __future__ import annotations

import json
import logging
import re
from typing import Any

import requests

from crmbrain.config import STAGE, Settings, redact_secrets, resolve_gemini_model
from crmbrain.models import Engagement

logger = logging.getLogger(__name__)

EXTRACT_TEXT_CAP = 200_000
_GEMINI_KEY_WARNED = False

EXTRACT_PROMPT = """You extract relationship-selling facts for a CRM.

Return ONLY JSON with this shape:
{
  "personal_details": "short paragraph Josh can skim",
  "family_notes": "",
  "relationship_hooks": "",
  "pain_points": "",
  "buying_committee": "",
  "gift_ideas": "",
  "birthday": "YYYY-MM-DD or empty",
  "stage_hint": "discovery_scheduled|discovery_completed|proposal_sent|signed|paid|no_show|nurture|closed_lost|",
  // Use signed when THIS person is in an active paid POC/pilot/kickoff/onboarding.
  "ticker_reason": "kicked_can|no_show|never_booked|deal_died|",
  "amount_hint": "",
  "deal_amount": "",
  "deal_terms": {
    "monthly_fee": "",
    "term_months": "",
    "one_time_fee": "",
    "poc_fee": "",
    "range_low": "",
    "range_high": "",
    "tcv": "",
    "offer_type": "retainer|pilot|success_fee|",
    "status": "quoted|accepted|declined|deferred|",
    "next_step_date": "",
    "quote": "verbatim phrase from the source that states the price"
  },
  "reminders": [{"when": "YYYY-MM-DD", "why": ""}]
}

Rules:
- Only facts the person actually said or that are obvious from the meeting.
- Birthday, kids, spouse, school, sports, city, hobbies matter.
- stage_hint only with clear evidence.
- Never set stage_hint to no_show for a meeting that has a transcript. A held call is discovery_completed.
- Use proposal_sent when a proposal/SOW/pricing was promised or sent on a held, priced call.
- ticker_reason if they punted, no-showed, or the deal died.
- deal_terms / amount_hint / deal_amount: THIS deal's price only.
  A price stated alongside the meeting guarantee is valid (that is the fee).
  Never invent. Never use Josh's case-study stats ($2M pipeline, $100K closed).
  quote must be a verbatim snippet from SOURCE.
  tcv is total contract value. If monthly_fee and term_months are known, tcv = monthly * term.
  If only a monthly range and a minimum term are known, tcv = range_low * min term.
- No dashes in gift_ideas.
"""

# Case-study / pitch language — never treat these as deal value.
# A price next to the meeting guarantee IS this deal's fee — do not suppress it.
_PITCH_HINTS = (
    "pipeline",
    "first 3 months",
    "first three months",
    "lead campaign",
    "10k lead",
    "free 10k",
    "keep working until",
    "replies per month",
    "airpods",
    "case study",
    "across our",
    "one of our",
)
_PRICE_HINTS = (
    "retainer",
    "per month",
    "/mo",
    "/month",
    "/ month",
    "a month",
    "each month",
    "monthly",
    "package",
    "proposal",
    "quoted",
    "quote",
    "our fee",
    "the fee",
    "investment",
    "pricing",
    "price",
    "one-time",
    "one time",
    "upfront",
    "invoice",
    "agreement",
    "sow",
    "would be",
    "that's $",
    "thats $",
    "pay",
    "cost us",
    "cost is",
    "minimum",
    "term",
    "x 3",
    "x3",
    "guarantee",
)
_MONEY_RE = re.compile(
    r"\$\s*(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*([kKmM])?"
    r"|(\d{1,3}(?:,\d{3})+)\s*([kKmM])?"
    r"|(\d+(?:\.\d+)?)\s*([kK])\b"
)
_RANGE_MO_RE = re.compile(
    r"\$?\s*(\d+(?:\.\d+)?)\s*([kK])?\s*[-–to]{1,3}\s*\$?\s*(\d+(?:\.\d+)?)\s*([kK])?"
    r"\s*(?:k\b)?\s*(?:/\s*mo|/month|per month|a month|monthly)",
    re.I,
)
_TIMES_RE = re.compile(
    r"\$?\s*(\d[\d,]*(?:\.\d+)?)\s*([kK])?\s*(?:x|×)\s*(\d+)\b",
    re.I,
)
_MONTHLY_FEE_RE = re.compile(
    r"\$?\s*(\d[\d,]*(?:\.\d+)?)\s*([kK])?\s*(?:/\s*mo|/month|per month|a month|monthly)",
    re.I,
)
_TERM_RE = re.compile(
    r"(\d+)\s*[- ]?(?:month|mo)s?\s*(?:minimum|min\.?|term|commit|agreement|retainer)?",
    re.I,
)
_ONE_TIME_RE = re.compile(
    r"\$?\s*(\d[\d,]*(?:\.\d+)?)\s*([kK])?\s*(?:one[ -]?time|package|flat|upfront)",
    re.I,
)
PROPOSAL_PROMISE_HINTS = (
    "proposal",
    "sow",
    "statement of work",
    "send pricing",
    "send you the",
    "i'll send",
    "i will send",
    "pricing to you",
    "quote to you",
)
AMOUNT_SOURCE_RANK = {
    "doc": 3,
    "completed_doc": 3,
    "payment": 3,
    "proposal_email": 2,
    "call": 1,
    "fireflies": 1,
    "cube_acr": 1,
    "gmail": 2,
}


def heuristic_extract(text: str) -> dict[str, Any]:
    blob = text or ""
    low = blob.lower()
    terms = heuristic_deal_terms(blob)
    amount = tcv_from_terms(terms) or parse_deal_amount(blob)
    facts = {
        "personal_details": "",
        "family_notes": "",
        "relationship_hooks": "",
        "pain_points": "",
        "buying_committee": "",
        "gift_ideas": "",
        "birthday": "",
        "stage_hint": "",
        "ticker_reason": "",
        "amount_hint": amount,
        "deal_amount": amount,
        "deal_terms": terms,
        "reminders": [],
    }
    birthday = re.search(r"\b(?:birthday|born on|bday)\b[^\n.]{0,40}(\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?)", blob, re.I)
    if birthday:
        facts["birthday"] = birthday.group(1)
        facts["personal_details"] = f"Birthday mentioned: {birthday.group(1)}"
        facts["reminders"].append({"when": birthday.group(1), "why": "birthday"})
    kid = re.search(r"\b(son|daughter|kids?|wife|husband|spouse)\b[^\n.]{0,80}", blob, re.I)
    if kid:
        facts["family_notes"] = kid.group(0)
    college = re.search(r"\b([A-Z][A-Za-z]+(?:\s[A-Z][A-Za-z]+){0,3})\s+(University|College)\b", blob)
    if college:
        facts["relationship_hooks"] = college.group(0)
    if any(w in low for w in ("no-show", "no show", "didn't show", "did not show")):
        facts["stage_hint"] = "no_show"
        facts["ticker_reason"] = "no_show"
    if any(w in low for w in ("circle back", "kick the can", "next quarter", "not right now", "reach back out in")):
        facts["ticker_reason"] = facts["ticker_reason"] or "kicked_can"
        facts["stage_hint"] = facts["stage_hint"] or "nurture"
    if any(w in low for w in ("we're going with someone", "deal is dead", "not moving forward", "out of budget")):
        facts["ticker_reason"] = "deal_died"
        facts["stage_hint"] = "closed_lost"
    if amount and any(h in low for h in PROPOSAL_PROMISE_HINTS):
        if not facts["stage_hint"] or facts["stage_hint"] in {"discovery_completed", "discovery_scheduled"}:
            facts["stage_hint"] = "proposal_sent"
    return facts


def _money_value(num: str, suffix: str) -> float | None:
    try:
        val = float(num.replace(",", ""))
    except ValueError:
        return None
    suf = (suffix or "").lower()
    if suf == "k":
        val *= 1000
    elif suf == "m":
        val *= 1_000_000
    return val


def format_amount(val: float) -> str:
    if val < 50 or val > 500_000:
        return ""
    if abs(val - round(val)) < 0.001:
        return str(int(round(val)))
    return f"{val:.2f}".rstrip("0").rstrip(".")


def _as_amount(raw: object) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    cleaned = re.sub(r"[^\d.]", "", text)
    try:
        return format_amount(float(cleaned))
    except ValueError:
        return ""


def _as_int(raw: object) -> int | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        val = int(float(re.sub(r"[^\d.]", "", text) or "nan"))
    except ValueError:
        return None
    if val < 1 or val > 60:
        return None
    return val


def _window_has(text: str, needles: tuple[str, ...]) -> bool:
    return any(n in text for n in needles)


def heuristic_deal_terms(text: str) -> dict[str, Any]:
    """Pull monthly / term / range / one-time figures from SOURCE text."""
    terms = {
        "monthly_fee": "",
        "term_months": "",
        "one_time_fee": "",
        "poc_fee": "",
        "range_low": "",
        "range_high": "",
        "tcv": "",
        "offer_type": "",
        "status": "",
        "next_step_date": "",
        "quote": "",
    }
    if not text:
        return terms
    low = text.lower()
    term = None
    tm = _TERM_RE.search(text)
    if tm:
        try:
            term = int(tm.group(1))
        except ValueError:
            term = None
    times = _TIMES_RE.search(text)
    if times:
        val = _money_value(times.group(1), times.group(2) or "")
        try:
            n = int(times.group(3))
        except ValueError:
            n = 0
        if val is not None and n:
            formatted = format_amount(val)
            if formatted:
                terms["monthly_fee"] = formatted
            if not term:
                term = n
            start = max(0, times.start() - 20)
            end = min(len(text), times.end() + 20)
            terms["quote"] = text[start:end].strip()
    rng = _RANGE_MO_RE.search(text)
    if rng:
        low_v = _money_value(rng.group(1), rng.group(2) or "k" if "k" in rng.group(0).lower() else "")
        high_v = _money_value(rng.group(3), rng.group(4) or "k" if "k" in rng.group(0).lower() else "")
        # $3-4k/mo — the k often applies to both sides.
        if "k" in rng.group(0).lower():
            if low_v is not None and low_v < 1000:
                low_v *= 1000
            if high_v is not None and high_v < 1000:
                high_v *= 1000
        if low_v is not None:
            terms["range_low"] = format_amount(low_v) or terms["range_low"]
            terms["monthly_fee"] = terms["monthly_fee"] or terms["range_low"]
        if high_v is not None:
            terms["range_high"] = format_amount(high_v) or terms["range_high"]
        start = max(0, rng.start() - 12)
        end = min(len(text), rng.end() + 24)
        terms["quote"] = terms["quote"] or text[start:end].strip()
    if not terms["monthly_fee"]:
        monthly = _MONTHLY_FEE_RE.search(text)
        if monthly:
            val = _money_value(monthly.group(1), monthly.group(2) or "")
            if val is not None:
                terms["monthly_fee"] = format_amount(val) or ""
                start = max(0, monthly.start() - 12)
                end = min(len(text), monthly.end() + 16)
                terms["quote"] = terms["quote"] or text[start:end].strip()
    one = _ONE_TIME_RE.search(text)
    if one:
        val = _money_value(one.group(1), one.group(2) or "")
        if val is not None:
            terms["one_time_fee"] = format_amount(val) or ""
            if not terms["quote"]:
                start = max(0, one.start() - 12)
                end = min(len(text), one.end() + 16)
                terms["quote"] = text[start:end].strip()
    if term:
        terms["term_months"] = str(term)
    if "retainer" in low or terms["monthly_fee"]:
        terms["offer_type"] = "retainer"
    elif "pilot" in low or "poc" in low:
        terms["offer_type"] = "pilot"
    elif "success fee" in low:
        terms["offer_type"] = "success_fee"
    if any(h in low for h in ("accepted", "we're in", "lets do it", "let's do it", "signed")):
        terms["status"] = "accepted"
    elif any(h in low for h in ("proposal", "quoted", "quote", "pricing")):
        terms["status"] = "quoted"
    tcv = tcv_from_terms(terms)
    if tcv:
        terms["tcv"] = tcv
    return terms


def tcv_from_terms(terms: dict[str, Any] | None) -> str:
    """Amount = TCV: tcv, else monthly*term, else range_low*min term."""
    terms = terms or {}
    direct = _as_amount(terms.get("tcv"))
    if direct:
        return direct
    monthly = _as_amount(terms.get("monthly_fee"))
    term = _as_int(terms.get("term_months"))
    if monthly and term:
        try:
            return format_amount(float(monthly) * term)
        except ValueError:
            pass
    low = _as_amount(terms.get("range_low"))
    if low and term:
        try:
            return format_amount(float(low) * term)
        except ValueError:
            pass
    one = _as_amount(terms.get("one_time_fee")) or _as_amount(terms.get("poc_fee"))
    if one:
        return one
    if monthly:
        return monthly
    if low:
        return low
    return ""


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")
_QUOTE_CUT_RES = (
    re.compile(r"\nOn .{0,160}wrote:\s*", re.I),
    re.compile(r"\n-{2,}\s*Original Message\b", re.I),
    re.compile(r"\nFrom:\s", re.I),
    re.compile(r"\nSent:\s", re.I),
)
_INSTALMENT_SENT_RE = re.compile(r"install?ment|this payment|partial payment|per month due", re.I)
_TOTAL_SENT_RE = re.compile(
    r"(?:total|in full|full amount|contract (?:value|total)|agreement total|engagement is|package is)",
    re.I,
)


def priced_sentences(text: str) -> list[str]:
    """Sentences that contain a price. Regex amounts never look outside these."""
    out: list[str] = []
    for part in _SENTENCE_SPLIT_RE.split(text or ""):
        sentence = part.strip()
        if sentence and _MONEY_RE.search(sentence):
            out.append(sentence)
    return out


def _figures_in_sentence(sentence: str) -> list[str]:
    if not sentence or _window_has(sentence.lower(), _PITCH_HINTS):
        return []
    hits: list[str] = []
    for match in _MONEY_RE.finditer(sentence):
        num = match.group(1) or match.group(3) or match.group(5)
        suffix = match.group(2) or match.group(4) or match.group(6) or ""
        val = _money_value(num, suffix)
        if val is None:
            continue
        formatted = format_amount(val)
        if formatted:
            hits.append(formatted)
    return list(dict.fromkeys(hits))


def parse_deal_amount(text: str) -> str:
    """One total from priced sentences. Skip if ambiguous. Never pick the largest figure."""
    if not text:
        return ""
    priced = priced_sentences(text)
    if not priced:
        return ""
    hits: list[str] = []
    for sentence in priced:
        if _window_has(sentence.lower(), _PITCH_HINTS):
            continue
        terms = heuristic_deal_terms(sentence)
        computed = tcv_from_terms(terms)
        if re.search(r"\bor\b", sentence.lower()) and not terms.get("term_months"):
            computed = ""
        if computed:
            hits.append(computed)
            continue
        figs = _figures_in_sentence(sentence)
        if len(figs) == 1:
            hits.append(figs[0])
        elif len(figs) > 1:
            return ""
    unique = list(dict.fromkeys(hits))
    if len(unique) == 1:
        return unique[0]
    return ""


def josh_new_text(text: str) -> str:
    """Josh's new reply only — drop quoted history / Original Message / > lines."""
    if not text:
        return ""
    cut = text
    earliest = None
    for pat in _QUOTE_CUT_RES:
        match = pat.search(cut)
        if match and (earliest is None or match.start() < earliest):
            earliest = match.start()
    if earliest is not None:
        cut = cut[:earliest]
    lines = []
    for line in cut.splitlines():
        if line.lstrip().startswith(">"):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def latest_proposal_figure(text: str) -> str:
    """Latest total in Josh's text. Ignore instalment amounts when a total exists."""
    scoped = josh_new_text(text) or (text or "")
    priced = priced_sentences(scoped)
    if not priced:
        return ""
    totals: list[str] = []
    instalments: list[str] = []
    for sentence in priced:
        amt = parse_deal_amount(sentence)
        if not amt:
            figs = _figures_in_sentence(sentence)
            amt = figs[-1] if len(figs) == 1 else ""
        if not amt:
            continue
        if _INSTALMENT_SENT_RE.search(sentence) and not _TOTAL_SENT_RE.search(sentence):
            instalments.append(amt)
        else:
            totals.append(amt)
    if totals:
        return totals[-1]
    unique_inst = list(dict.fromkeys(instalments))
    if len(unique_inst) == 1:
        return unique_inst[0]
    return ""


def _norm_fuzzy(text: str) -> str:
    compact = (text or "").lower()
    compact = compact.replace(",", "").replace("$", " ")
    compact = re.sub(r"\s+", " ", compact).strip()
    return compact


def quote_matches_source(quote: str, source: str) -> bool:
    """True when the model's verbatim quote fuzzy-matches SOURCE."""
    q = _norm_fuzzy(quote)
    s = _norm_fuzzy(source)
    if not q or not s or len(q) < 4:
        return False
    if q in s:
        return True
    tokens = [t for t in q.split() if t not in {"the", "a", "an", "to", "for", "of", "and"}]
    if len(tokens) < 3:
        return False
    found = sum(1 for t in tokens if t in s)
    return found / len(tokens) >= 0.7


def amount_attested_in_text(text: str, amount: str) -> bool:
    """Digits, $Nk, or a monthly*term / range that equals amount."""
    if not text or not amount:
        return False
    compact = text.replace(",", "")
    try:
        n = float(amount)
    except ValueError:
        return False
    n_int = int(n) if abs(n - round(n)) < 0.001 else None
    if n_int is not None and re.search(rf"\$?\s*{n_int}(?:\.0+)?\b", compact):
        return True
    if n_int is not None and n_int >= 1000 and n_int % 1000 == 0 and re.search(
        rf"\$?\s*{n_int // 1000}\s*k\b", compact, re.I
    ):
        return True
    terms = heuristic_deal_terms(text)
    computed = tcv_from_terms(terms)
    if computed and amounts_equal(computed, amount):
        return True
    return False


def normalize_amount_hint(
    hint: object,
    text: str,
    *,
    quote: str = "",
    terms: dict[str, Any] | None = None,
) -> str:
    """Keep a model amount when its quote fuzzy-matches SOURCE, else heuristic TCV."""
    terms = terms if isinstance(terms, dict) else {}
    heuristic = tcv_from_terms(terms) or parse_deal_amount(text)
    raw = str(hint or "").strip()
    if not raw:
        return heuristic
    parsed = parse_deal_amount(raw) or parse_deal_amount(f"${raw}")
    if not parsed:
        cleaned = re.sub(r"[^\d.]", "", raw)
        try:
            parsed = format_amount(float(cleaned))
        except ValueError:
            parsed = ""
    if not parsed:
        return heuristic
    q = quote or str(terms.get("quote") or "")
    if q and quote_matches_source(q, text):
        return parsed
    if amount_attested_in_text(text, parsed):
        return parsed
    return heuristic


def amount_source_kind(ev: Engagement | None, explicit: str = "") -> str:
    if explicit:
        return explicit
    extra = (ev.extra if ev else {}) or {}
    raw = str(extra.get("amount_source") or "").strip().lower()
    if raw:
        return raw
    if not ev:
        return "call"
    if ev.source == "gmail":
        stage = str(ev.stage_hint or extra.get("stage") or "")
        if stage == STAGE["paid"] or extra.get("payment"):
            return "payment"
        if extra.get("document_id") or extra.get("document_name") or extra.get("completed_doc"):
            return "doc" if extra.get("completed_doc") or stage == STAGE["signed"] else "proposal_email"
        if extra.get("josh_sent_proposal") or stage == STAGE["proposal_sent"]:
            return "proposal_email"
        if extra.get("payment"):
            return "payment"
        return "proposal_email"
    if ev.source in {"fireflies", "cube_acr", "allo"}:
        return "call"
    return ev.source or "call"


def infer_existing_amount_source(stage: str = "") -> str:
    if stage == STAGE["paid"]:
        return "payment"
    if stage == STAGE["signed"]:
        return "doc"
    if stage == STAGE["proposal_sent"]:
        return "proposal_email"
    return "call"


def amount_to_write(
    current_amount: object,
    hint: str,
    *,
    stage: str = "",
    incoming_source: str = "",
    existing_source: str = "",
    is_instalment: bool = False,
) -> str:
    """Write TCV. Higher-priority newer source may overwrite open stages.

    Paid amounts change only from doc/payment evidence. An instalment never
    replaces a larger existing total.
    """
    if not hint:
        return ""
    cur = str(current_amount or "").strip()
    empty = (not cur) or cur in {"0", "0.0", "0.00"}
    if not empty and amounts_equal(cur, hint):
        return ""
    incoming = incoming_source or "call"
    existing = existing_source or (infer_existing_amount_source(stage) if not empty else "")
    paid = stage == STAGE["paid"] or incoming == "payment" and existing == "payment" and not empty
    if stage == STAGE["paid"] or existing == "payment":
        paid = True
    if paid:
        if incoming not in {"doc", "completed_doc", "payment"}:
            return ""
        if not empty:
            try:
                if float(str(hint).replace(",", "")) + 0.01 < float(str(cur).replace(",", "")):
                    return ""
            except ValueError:
                return ""
            if is_instalment:
                return ""
        return hint if empty or incoming in {"doc", "completed_doc", "payment"} else ""
    if empty:
        return hint
    inc = AMOUNT_SOURCE_RANK.get(incoming, 0)
    ex = AMOUNT_SOURCE_RANK.get(existing, 0) if existing else 0
    if inc > ex:
        return hint
    return ""


def deal_amount_to_write(deal: dict | None, amount: str, ev: Engagement | None = None) -> str:
    props = (deal or {}).get("properties") or {}
    extra = (ev.extra if ev else {}) or {}
    return amount_to_write(
        props.get("amount"),
        amount,
        stage=str(props.get("dealstage") or ""),
        incoming_source=amount_source_kind(ev),
        existing_source=str(props.get("amount_source") or extra.get("existing_amount_source") or ""),
        is_instalment=bool(extra.get("amount_is_instalment")),
    )


def amount_citation_note(ev: Engagement | None, amount: str, terms: dict[str, Any] | None = None) -> str:
    extra = (ev.extra if ev else {}) or {}
    terms = terms if isinstance(terms, dict) else (extra.get("deal_terms") or {})
    source = amount_source_kind(ev)
    monthly = str(terms.get("monthly_fee") or "").strip()
    term = str(terms.get("term_months") or "").strip()
    bits = [f"Deal amount ${amount} from {source}"]
    if monthly and term:
        bits.append(f"{monthly} x {term} months")
    elif monthly:
        bits.append(f"monthly fee {monthly}")
    quote = str(terms.get("quote") or "").strip()
    if quote:
        bits.append(f'Quote: "{quote[:240]}"')
    if ev and (ev.source or ev.raw_subject):
        bits.append(f"{ev.source}: {ev.raw_subject or ev.external_id}".strip())
    return "\n".join(bits)


def amounts_equal(left: object, right: object) -> bool:
    """HubSpot often returns 3000.0 for a 3000 write."""
    if left is None or right is None:
        return False
    a = str(left).strip().replace(",", "")
    b = str(right).strip().replace(",", "")
    if not a or not b:
        return False
    if a == b:
        return True
    try:
        return abs(float(a) - float(b)) < 0.01
    except ValueError:
        return False


def merge_fact_dicts(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Gemini empty strings must not wipe heuristic facts."""
    out = dict(base)
    for key, value in (incoming or {}).items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if value in ([], {}):
            continue
        if key == "deal_terms" and isinstance(value, dict):
            merged = dict(out.get("deal_terms") or {})
            for sub_k, sub_v in value.items():
                if sub_v is None:
                    continue
                if isinstance(sub_v, str) and not sub_v.strip():
                    continue
                merged[sub_k] = sub_v
            out["deal_terms"] = merged
            continue
        out[key] = value
    return out


def _is_silent_source(ev: Engagement) -> bool:
    extra = ev.extra or {}
    if extra.get("silent_meeting") is True:
        return True
    status = str(extra.get("summary_status") or "").lower()
    if "silent" in status:
        return True
    if extra.get("sentence_count") == 0 or extra.get("has_sentences") is False:
        return True
    return False


def extraction_text(ev: Engagement) -> str:
    """Overview + shorthand + action items + full transcript, capped for Gemini 2.5 Flash."""
    extra = ev.extra or {}
    parts: list[str] = []
    for label, key in (
        ("OVERVIEW", "overview"),
        ("SHORTHAND", "shorthand_bullet"),
        ("ACTION ITEMS", "action_items"),
    ):
        raw = extra.get(key)
        if not raw and label == "OVERVIEW":
            raw = ev.summary
        if not raw:
            continue
        if isinstance(raw, list):
            raw = "\n".join(str(x) for x in raw if x)
        raw = str(raw).strip()
        if raw:
            parts.append(f"{label}:\n{raw}")
    if ev.summary and "OVERVIEW:" not in "\n".join(parts):
        parts.insert(0, f"OVERVIEW:\n{ev.summary.strip()}")
    if ev.transcript:
        parts.append(f"TRANSCRIPT:\n{ev.transcript}")
    if ev.raw_subject:
        parts.append(f"SUBJECT:\n{ev.raw_subject}")
    return "\n\n".join(parts)[:EXTRACT_TEXT_CAP]


CALL_SOURCES = frozenset({"fireflies", "cube_acr", "allo"})


def _call_amount_from_gemini(facts: dict[str, Any], text: str) -> str:
    """Call amounts require a Gemini deal_terms result with a validated quote."""
    terms = facts.get("deal_terms") if isinstance(facts.get("deal_terms"), dict) else {}
    quote = str(terms.get("quote") or "")
    if not quote or not quote_matches_source(quote, text):
        return ""
    amount = (
        tcv_from_terms(terms)
        or _as_amount(facts.get("amount_hint"))
        or _as_amount(facts.get("deal_amount"))
        or _as_amount(terms.get("tcv"))
    )
    return amount or ""


def extract(settings: Settings, ev: Engagement) -> dict[str, Any]:
    global _GEMINI_KEY_WARNED
    raw_text = extraction_text(ev)
    extra = ev.extra or {}
    if ev.source == "gmail" and extra.get("josh_sent_proposal"):
        text = josh_new_text(raw_text) or raw_text
    else:
        text = raw_text
    facts = heuristic_extract(text)
    gemini_ok = False
    if not getattr(settings, "gemini_key", ""):
        if text.strip() and not _GEMINI_KEY_WARNED:
            logger.warning("gemini key missing; heuristic extract only")
            _GEMINI_KEY_WARNED = True
    elif text.strip():
        try:
            facts = merge_fact_dicts(facts, _gemini(settings, text))
            gemini_ok = True
        except Exception as exc:
            logger.warning("gemini extract failed: %s", redact_secrets(str(exc)))
    if ev.stage_hint:
        facts["stage_hint"] = facts.get("stage_hint") or ev.stage_hint
    silent = _is_silent_source(ev)
    if ev.source in CALL_SOURCES and not silent:
        hint = str(facts.get("stage_hint") or "").strip().lower()
        if hint in {"no_show", STAGE["no_show"]}:
            facts["stage_hint"] = "discovery_completed"
        if str(facts.get("ticker_reason") or "").strip().lower() == "no_show":
            facts["ticker_reason"] = ""
    terms = facts.get("deal_terms") if isinstance(facts.get("deal_terms"), dict) else {}
    if ev.source in CALL_SOURCES:
        amount = _call_amount_from_gemini(facts, text) if gemini_ok else ""
    elif extra.get("josh_sent_proposal"):
        amount = latest_proposal_figure(text)
        if not amount and gemini_ok:
            amount = _call_amount_from_gemini(facts, text)
    else:
        amount = ""
        if gemini_ok:
            amount = _call_amount_from_gemini(facts, text)
        if not amount:
            amount = parse_deal_amount(text)
    facts["amount_hint"] = amount
    facts["deal_amount"] = amount
    if amount and isinstance(terms, dict) and not terms.get("tcv"):
        terms = dict(terms)
        terms["tcv"] = amount
        facts["deal_terms"] = terms
    if ev.source in {"fireflies", "cube_acr"} and not any(
        str(facts.get(k) or "").strip()
        for k in ("personal_details", "family_notes", "relationship_hooks")
    ):
        overview = (ev.summary or "").strip()
        if overview:
            facts["personal_details"] = overview[:800]
    return facts


def _gemini(settings: Settings, text: str) -> dict[str, Any]:
    model = resolve_gemini_model(getattr(settings, "gemini_model", "") or "")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    resp = requests.post(
        url,
        params={"key": settings.gemini_key},
        json={
            "contents": [{"parts": [{"text": EXTRACT_PROMPT + "\n\nSOURCE:\n" + text}]}],
            "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
        },
        timeout=90,
    )
    try:
        resp.raise_for_status()
    except Exception as exc:
        raise RuntimeError(redact_secrets(str(exc))) from None
    body = resp.json()
    raw = body["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(raw)


def merge_contact_props(existing: dict, facts: dict[str, Any]) -> dict[str, str]:
    def join(old: str, new: str) -> str:
        new = (new or "").strip()
        old = (old or "").strip()
        if not new:
            return old
        if new in old:
            return old
        return (old + "\n" + new).strip() if old else new

    props = existing.get("properties") or {}
    out = {
        "personal_details": join(props.get("personal_details", ""), facts.get("personal_details", "")),
        "family_notes": join(props.get("family_notes", ""), facts.get("family_notes", "")),
        "relationship_hooks": join(props.get("relationship_hooks", ""), facts.get("relationship_hooks", "")),
        "pain_points": join(props.get("pain_points", ""), facts.get("pain_points", "")),
        "buying_committee": join(props.get("buying_committee", ""), facts.get("buying_committee", "")),
        "gift_ideas": join(props.get("gift_ideas", ""), facts.get("gift_ideas", "")),
    }
    if facts.get("birthday") and re.match(r"\d{4}-\d{2}-\d{2}", facts["birthday"]):
        out["date_of_birth"] = facts["birthday"]
    return {k: v for k, v in out.items() if v}


def stage_id(hint: str) -> str:
    if hint in STAGE.values():
        return hint
    return STAGE.get(hint, "")
