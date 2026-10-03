# crmbrain stage spec (HubSpot migration of Sat Oct 3, 2026)
Portal 245860747. CRMBRAIN_DRY_RUN=1 is set on Railway service crmbrain (production) until crmbrain is updated to this spec.

## 1. Old -> new map, Sales Pipeline (id `default`)
| Old key in config.STAGE | Old stage (id) | New stage (id) | Prob | Closed |
|---|---|---|---|---|
| replied | Replied (`appointmentscheduled`) | **Initial Interest** (`appointmentscheduled`, renamed) | 5% | no |
| discovery_scheduled | Discovery Scheduled (`qualifiedtobuy`) | **Meeting Booked** (`qualifiedtobuy`, renamed) | 10% | no |
| discovery_completed | Discovery Completed (`presentationscheduled`) | **Discovery Held** (`presentationscheduled`, renamed) | 20% | no |
| proposal_sent | Proposal Sent (`decisionmakerboughtin`) | **Proposal Sent** (`decisionmakerboughtin`) | 40% | no |
| (new) | n/a | **Needs Stakeholder Approval** (`4391745240`) | 45% | no |
| signed | Signed (`closedwon`) | **Contract Sent / Signed, Not Yet Paid** (`4391699184`) | 80% | no |
| (new) | n/a | **POC** (`4391745241`) | 85% | no |
| paid | Paid (`3482933986`, DELETED) | **Closed Won** (`closedwon`, renamed) | 100% | won |
| closed_lost | Closed Lost (`closedlost`) | **Closed Lost** (`closedlost`) | 0% | lost |
| nurture | Nurture (`3486952153`) | **Nurture** (`3486952153`, now closed) | 0% | lost-type |
| no_show | No Show (`3557889773`, DELETED) | none: use `no_show_count` property | n/a | n/a |

Important: `closedwon` used to mean "Signed" and now means **Closed Won = payment received**. `signed` must point to `4391699184`. Display order: 10..100 in the table order.

Suggested new config.STAGE:
```python
STAGE = {
    "initial_interest": "appointmentscheduled",
    "meeting_booked": "qualifiedtobuy",
    "discovery_held": "presentationscheduled",
    "proposal_sent": "decisionmakerboughtin",
    "needs_stakeholder_approval": "4391745240",
    "contract_signed_unpaid": "4391699184",
    "poc": "4391745241",
    "closed_won": "closedwon",
    "closed_lost": "closedlost",
    "nurture": "3486952153",
}
RENEWAL_PIPELINE = "2604181234"
RENEWAL_STAGE = {
    "renewal_upcoming": "4391699185",
    "call_scheduled": "4391699186",
    "at_risk": "4391699187",
    "renewed": "4392753853",   # closed won
    "churned": "4392753854",   # closed lost
}
```
Suggested STAGE_RANK: closed_lost 0, nurture 1, initial_interest 2, meeting_booked 3, discovery_held 4, proposal_sent 5, needs_stakeholder_approval 5 (peer of proposal_sent; either can follow Discovery Held, so moving between them is not "backward"), contract_signed_unpaid 6, poc 7, closed_won 8.
CLOSED_WON_STAGES = {closed_won}; MONEY_STAGES = {proposal_sent, needs_stakeholder_approval, contract_signed_unpaid, poc, closed_won}; MEETING_STAGES = meeting_booked..closed_won; PRE_SALE_STAGES = {initial_interest, meeting_booked, discovery_held}; BACK_STAGES = {nurture, closed_lost}.

## 2. Detection rules, new-business pipeline
| Stage | Enter when (observable evidence) | Notes |
|---|---|---|
| Initial Interest | Positive reply from **Smartlead** (lead category Interested / Meeting Request / Info Request: 1, 2, 5), **HeyReach** (positive reply), or **Allo** (positive SMS/call reply). | Replaces Replied. Only create a deal for non-excluded contacts. Dedupe by contact/company before creating. |
| Meeting Booked | Future Google Calendar/Calendly/Zoom event with a non-excluded external prospect attendee, or a booking-confirmation email. | If the meeting passes with no transcript, or Fireflies shows a silent meeting of 13 min or less with no summary: increment `no_show_count`, stay in Meeting Booked. Never move the stage on a no-show. |
| Discovery Held | Fireflies or Cube ACR transcript of 15 min or more with the prospect, with real conversation (summary_status processed). | Extra discovery calls stay here. A transcript with a current client is not discovery (skip if the company has a Closed Won deal or an open Client Renewals deal). |
| Proposal Sent | Josh sends pricing in writing: email to the contact with proposal/recap/pricing/rundown plus a $ figure or attachment, or a Fireflies action item "send proposal/SOW/pricing" with pricing in the summary. | Set amount if a figure is present. |
| Needs Stakeholder Approval | Transcript or email states the buyer needs partner/board/leadership/procurement/silent-partner sign-off (e.g. "present to partners/board", "review with my partner", "needs leadership approval"). | Can follow Discovery Held or Proposal Sent. Moving between it and Proposal Sent is lateral. |
| Contract Sent / Signed, Not Yet Paid | PandaDoc/DocuSign **sent** or **completed**, payment link or invoice sent, or transcript with explicit verbal yes ("agreed to proceed"), and no client payment yet. | `documents.stage_from_signature_mail`: completed signature now maps HERE, not to closedwon. |
| POC | After signature, a proof of concept is running: signed POC/pilot SOW (e.g. $0 or pilot SOW) and campaign activity for the client (prospect-reply forwards to the client, portal sync, onboarding call) while the paid agreement is not yet paid. Set `sg_deal_type` = free_poc or paid_poc. | Paid POCs that have already paid go to Closed Won with `sg_deal_type`=paid_poc. |
| Closed Won | First client payment received: HubSpot Payments email "You received a $X payment" / "Your invoice has been paid" from the contact's address, Stripe receipt, or Josh-logged check/ACH (check payers like Emcor). | Keep existing closedate when moving; set `monthly_fee`, `contract_months` if known. Never move a Closed Won deal back; churn goes to Client Renewals. |
| Closed Lost | Explicit "no", Josh disqualifies, or Josh confirms. Must set `lost_reason` (prospect_dq, josh_dq, not_a_fit, budget_timing, went_dark, other). | crmbrain should only SUGGEST Closed Lost (Slack/review queue), e.g. 2 no-shows with no reschedule in 14 days, or contract unpaid 21+ days. |
| Nurture | Only when the call/emails show a concrete fit reason and a timing/approval reason it is paused. Must set `nurture_reason`. | If fit is unclear, do NOT nurture: set `josh_review_flag` and leave the stage. Reopen to Meeting Booked/Discovery Held when a new meeting is booked. |

## 3. Client Renewals pipeline (`2604181234`)
| Stage (id) | Prob | Enter when |
|---|---|---|
| Renewal Upcoming (`4391699185`) | 60% | Auto-create for each active client 30 days before `contract_end_date` (one open renewal deal per client). Copy contact/company associations, `monthly_fee`. |
| Call Scheduled (`4391699186`) | 70% | Calendar event with the client titled renewal/review, scheduled ~2 weeks before `contract_end_date` (the EA owns scheduling; crmbrain should alert the EA if none exists 14 days out). |
| At Risk (`4391699187`) | 35% | `positive_replies_30d` < 12 (from Smartlead campaign stats for that client's campaigns), or client asks to pause/cancel, or failed payment unresolved 7+ days. |
| Renewed (`4392753853`) | won | Payment received for the new term/extension. |
| Churned (`4392753854`) | lost | Billing stops past the end date or client cancels. |
crmbrain should refresh `positive_replies_30d` daily for active clients from Smartlead and move Renewal Upcoming/Call Scheduled deals to At Risk below 12.

## 4. Properties (all on deals, group dealinformation)
`lost_reason` (select: prospect_dq, josh_dq, not_a_fit, budget_timing, went_dark, other), `nurture_reason` (text), `sg_deal_type` (select: new_business, paid_poc, free_poc, renewal, expansion), `monthly_fee` (number), `contract_months` (number), `contract_end_date` (date), `no_show_count` (number), `positive_replies_30d` (number), `josh_review_flag` (text; non-empty = needs Josh). Existing `crmbrain_locked` stays: never change locked deals' values except a stage migration.

## 5. Code touchpoints
config.STAGE + new RENEWAL_* maps; policy.py (STAGE_RANK and stage sets, 49 refs); reconcile.py (42); cycle.py (9); ticker.py (6); sources/gmail_scan.py (5: add proposal-sent and contract-sent detection, payment notice -> closed_won); budget.py, intent.py, evidence.py (4 each: stage_hint enums, no_show becomes a counter); documents.py (3: signature completed -> contract_signed_unpaid); hubspot.py (2: preserve closedate on closed moves); intelligence.py and prune.py (1 each); LLM prompts' stage_hint enum lists; tests. Remove every reference to `3482933986` (Paid) and `3557889773` (No Show): both stages are deleted and writes to them will fail.
