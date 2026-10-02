"""PandaDoc / DocuSign / Stripe status from Gmail.

sent/viewed → Proposal Sent (+ amount when stated).
completed + paid agreement → Signed.
completed free SOW / $0 / complimentary → not Signed.
"""

from __future__ import annotations

import re

from crmbrain.config import STAGE
from crmbrain.intelligence import parse_deal_amount

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
