"""PandaDoc / DocuSign / Stripe status from Gmail.

sent/viewed → Proposal Sent (+ amount when stated).
completed + paid agreement → Signed.
completed free SOW / $0 / complimentary → not Signed.
"""

from __future__ import annotations

import re

from crmbrain.config import JOSH_EMAILS, STAGE, is_josh_address
from crmbrain.intelligence import format_amount, parse_deal_amount, _money_value, _MONEY_RE

FREE_DOC_HINTS = (
    "free sow",
    "free statement of work",
    "complimentary",
    "no charge",
    "unpaid sow",
    "free agreement",
)
# $0 / $0.00 as a whole amount only — "$21,000.00" is not free.
FREE_AMOUNT_RE = re.compile(r"\$0(?:\.00)?(?![\d,])")
# Josh is the payer. A client's paid trial/pilot is NOT this.
JOSH_PAYS_HINTS = (
    "contractor agreement",
    "contractor-agreement",
)
JOSH_PAYER_HINTS = (
    "josh pays",
    "paid by josh",
    "paid by salesglider",
    "salesglider will pay",
    "salesglider pays",
    "we'd pay you",
    "we would pay you",
    "josh is the payer",
)
PAID_DOC_HINTS = (
    "growth partners",
    "retainer",
    "agreement",
    "proposal",
    "order form",
    "invoice",
    "statement of work",
    "sow",
)
COMPLETED_HINTS = (
    "has been completed",
    "has been signed",
    "document completed",
    "completed the document",
    "signing is complete",
    "fully executed",
)
VIEWED_HINTS = ("viewed", "opened the document", "document was viewed")
SENT_HINTS = ("sent you", "document was sent", "has sent", "sent a document")


def is_signature_mail(subject: str, sender: str) -> bool:
    blob = f"{subject} {sender}".lower()
    return any(h in blob for h in ("pandadoc", "docusign"))


def is_payment_mail(subject: str, sender: str, snippet: str = "") -> bool:
    blob = f"{subject} {sender} {snippet}".lower()
    return "you received a payment" in blob or "payment received" in blob


AGREEMENT_PARTY_RE = re.compile(
    r"(?:updated\s+)?agreement:\s*(.+?)\s+x\s+salesglider",
    re.I,
)
PAYMENT_FROM_RE = re.compile(
    r"(?:you received a payment|payment received).{0,80}?\bfrom\s+([^.\n]{3,80})",
    re.I,
)
PAYER_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
TOTAL_HINT_RE = re.compile(
    r"(?:total|in full|full amount|contract (?:value|total)|agreement total)",
    re.I,
)
INSTALMENT_HINT_RE = re.compile(r"install?ment|this payment|partial payment", re.I)
_PAYMENT_SKIP_EMAIL = (
    "hubspot",
    "salesglider",
    "stripe.com",
    "quickbooks",
    "intuit.com",
    "noreply",
    "no-reply",
    "donotreply",
    "pandadoc",
    "docusign",
)
_COMPANY_TOKENS = (" group", " llc", " inc", " ltd", " energy", " partners", " company", " co.")


def _skip_payer_email(email: str) -> bool:
    low = (email or "").strip().lower()
    if not low or low in JOSH_EMAILS or is_josh_address(low):
        return True
    return any(h in low for h in _PAYMENT_SKIP_EMAIL)


def payer_emails_from_body(body: str) -> list[str]:
    """Payer emails live in the payment BODY; headers are HubSpot/Josh."""
    out: list[str] = []
    for match in PAYER_EMAIL_RE.finditer(body or ""):
        email = match.group(0).strip().lower()
        if _skip_payer_email(email) or email in out:
            continue
        out.append(email)
    return out


def payment_amount_from_text(text: str) -> tuple[str, bool]:
    """Return (amount, is_instalment). Prefer a stated total over one instalment."""
    blob = text or ""
    totals: list[str] = []
    others: list[str] = []
    for match in _MONEY_RE.finditer(blob):
        num = match.group(1) or match.group(3) or match.group(5)
        suffix = match.group(2) or match.group(4) or match.group(6) or ""
        val = _money_value(num, suffix)
        if val is None:
            continue
        formatted = format_amount(val)
        if not formatted:
            continue
        start = max(0, match.start() - 40)
        window = blob[start:match.end() + 8]
        if TOTAL_HINT_RE.search(window):
            totals.append(formatted)
        else:
            others.append(formatted)
    is_instalment = bool(INSTALMENT_HINT_RE.search(blob))
    if totals:
        try:
            return max(totals, key=lambda x: float(x)), False
        except ValueError:
            return totals[0], False
    parsed = parse_deal_amount(blob)
    if parsed:
        return parsed, is_instalment and len(set(others)) == 1
    if others:
        try:
            pick = max(others, key=lambda x: float(x))
        except ValueError:
            pick = others[0]
        return pick, is_instalment
    return "", is_instalment


def commerce_match_fields(subject: str, snippet: str = "", body: str = "") -> tuple[str, str, str]:
    """Payer name, company, amount from a payment or agreement mail."""
    text = f"{subject}\n{snippet}\n{body}"
    pay_amount, _instalment = payment_amount_from_text(text)
    amount = pay_amount or parse_deal_amount(text)
    company = ""
    party = AGREEMENT_PARTY_RE.search(text)
    if party:
        company = re.sub(r"\s+", " ", party.group(1)).strip(" -:|")
    payer = ""
    from_who = PAYMENT_FROM_RE.search(text)
    if from_who:
        payer = re.sub(r"\s+", " ", from_who.group(1)).strip(" -:|")
        if not company and any(tok in payer.lower() for tok in _COMPANY_TOKENS):
            company = payer
    return payer, company, amount


def looks_josh_pays_document(subject: str, body: str, document_name: str = "") -> bool:
    """True when the paper is a contractor agreement or Josh is the payer."""
    blob = f"{subject} {body} {document_name}".lower()
    if any(h in blob for h in JOSH_PAYS_HINTS):
        return True
    return any(h in blob for h in JOSH_PAYER_HINTS)


def looks_free_document(subject: str, body: str, document_name: str = "") -> bool:
    blob = f"{subject} {body} {document_name}".lower()
    if any(h in blob for h in FREE_DOC_HINTS):
        return True
    if FREE_AMOUNT_RE.search(blob):
        return True
    if re.search(r"\bfree\b.{0,24}\b(sow|statement of work|agreement)\b", blob):
        return True
    if re.search(r"\b(sow|statement of work|agreement)\b.{0,24}\bfree\b", blob):
        return True
    return False


def document_name(subject: str, body: str) -> str:
    text = f"{subject}\n{body}"
    for pattern in (
        r"(?:document|agreement|proposal)\s+[\"']([^\"']{3,120})[\"']",
        r"viewed\s+(.{3,120}?)\s+(?:on|in)\s+pandadoc",
        r"sent you\s+(.{3,120}?)(?:\.|$)",
        r"completed\s+(.{3,120}?)(?:\.|$)",
    ):
        match = re.search(pattern, text, re.I)
        if match:
            return re.sub(r"\s+", " ", match.group(1)).strip(" .:-")
    return ""


def stage_from_signature_mail(subject: str, sender: str, snippet: str, body: str = "") -> tuple[str, str, str]:
    """Return (stage_id, amount, document_name). Empty stage means ignore."""
    blob = f"{subject} {sender} {snippet} {body}".lower()
    name = document_name(subject, body or snippet)
    amount = parse_deal_amount(f"{subject}\n{body or snippet}")
    if looks_josh_pays_document(subject, body or snippet, name):
        return "", amount, name
    free = looks_free_document(subject, body or snippet, name)
    if is_payment_mail(subject, sender, snippet):
        return STAGE["paid"], amount, name
    if "docusign" in blob or "pandadoc" in blob:
        if any(h in blob for h in COMPLETED_HINTS):
            if free:
                return "", amount, name
            return STAGE["signed"], amount, name
        if any(h in blob for h in VIEWED_HINTS) or any(h in blob for h in SENT_HINTS):
            return STAGE["proposal_sent"], amount, name
    return "", amount, name
