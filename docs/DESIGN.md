# Snack Counter — design

**Scope of "automate".** I automate coordination, guardrails and reporting. I do not
automate payments or HR approval. A human still walks to the shop and pays; HR remains the
system of record for money. What changes is that every empty box has one named owner, a
deadline, and an auditable path to reimbursement — and finance gets one report.

## 1. The refill lifecycle

```
 any employee          system                  volunteer              HR (external)
 ────────────          ──────                  ─────────              ─────────────
 "box empty" ───────▶ OPEN  (budget reserved)
                         │
                         ├─▶ OFFERED (45 min) ──▶ accept ──▶ ASSIGNED
                         │      │ decline            │        due = next business
                         │      ▼                    │        day 18:00, office tz
                         │   next candidate          │
                         │      │ nobody             ▼
                         └─▶ BROADCAST ──▶ first accept ──▶ ASSIGNED
                                │ expires
                                ▼
                          OPEN / manager alert
                                             ASSIGNED
                                                │ receipt logged
                                                ▼
                                            PURCHASED ──▶ claim via outbox ──▶ PENDING
                                                │                                │ 1–10 days
                                                │                                ▼
                                                │                           APPROVED ──▶ REIMBURSED
                                                │                           REJECTED ──▶ NEEDS_ATTENTION
                                                │
                              overdue + 4h grace ──▶ OPEN, previous owner excluded, next candidate
```

State changes are conditional updates (compare-and-swap on `status` + `version`); the
database, not the application, decides who wins.

## 2. Parts and the data each keeps

| Part | Responsibility | Keeps |
|---|---|---|
| Chat adapter (`FakeChat`) | offers, reminders, escalations, audit trail for people | `messages` — channel, text, actions, refill id |
| Refill service | lifecycle, assignment, purchases | `refills`, `offers`, `refill_exclusions`, `purchases`, `purchase_lines`, `audit` |
| Policy | eligibility + fairness ranking | reads `employees`, `presence`, `fairness`, `claims`, `refills` |
| Budget ledger | reserve / release / spend per office-month | `budget_entries`, `budget_overrides` |
| HR adapter (`FakeHr`) | claims, 1–10 day decisions, payout in payroll currency | separate `hr_claims` table, own FX rate |
| Outbox + sweeper | retries, offer expiry, overdue hand-over, claim polling | `outbox`, `claims` |
| Reporting | one monthly view across both offices | reads `purchase_lines`, `budget_entries`, `fx_rates` |

`refills` holds `box_id, status, version, assignee_id, offered_to_id, due_at,
expected_cost_minor, currency, reserved_minor, blocked_reason, abandoned_count`.
`purchase_lines` stores the receipt currency, the office currency, the converted amount and
**the FX rate used**, so a report never has to re-derive history. Money is integer minor
units (øre / paise) everywhere — no floats.

Scale: ~35 volunteers, ~6 boxes, <100 claims/month. One process, one SQLite database, one
idempotent `tick()`. No broker, no workers, no microservices.

## 3. How the four failures are prevented

**"Everyone assumes someone else will refill it."** An empty report creates exactly one
coordination record, which is offered to a *named person* with a 45-minute window. Silence
is a failure state: the offer expires and moves on. Escalation is explicit: candidate → next
candidate → broadcast to everyone on site → manager alert. A box is never `OPEN` with nobody
responsible and no visible alert.

**"Two people buy on the same day."** Three layers:
1. an accept is `UPDATE refills SET status='ASSIGNED' … WHERE id=? AND version=? AND
   status IN ('OFFERED','BROADCAST')` — row count 0 means someone else already has it;
2. a partial unique index, `one_active_refill_per_box` on `(box_id)` where status is
   `OPEN/OFFERED/BROADCAST/ASSIGNED`, so even a buggy code path cannot create a second one;
3. reporting a box empty within 6 hours of a purchase is rejected (manager override path).
   Offline double-buys remain possible; the second claim is accepted and flagged rather than
   punishing a volunteer.

**"A few people do most refills and wait weeks for their money."** Ranking prefers the
fewest refills in the last 90 days, then the longest since the last one, then fewer
abandonments; a 7-day cooldown and a **maximum of two unreimbursed claims per person** stop
the same person fronting cash indefinitely. The claim is created at the moment of purchase,
so waiting starts from the receipt, not from someone remembering to file it.

**"Nobody knows spend or which snacks get eaten."** Purchase lines carry office, category,
receipt currency, office currency, FX rate and purchase-local date. The report shows spend
per office in its own currency, spend per category, outstanding claims, and one converted
grand total.

## 4. Currency and reporting rules

* Budgets are per office per calendar month **in the office's own currency**, so the
  enforcement check needs no FX. A purchase reserves an estimate (median of the last 10
  receipts, clamped to the stated typical range), then releases it and books the actual.
* The month is decided by the **purchase date in the office's timezone**, not by the
  approval date. A 31 Jan purchase approved on 10 Feb still belongs to January; the report
  shows submitted, approved and outstanding separately.
* Receipt currency ≠ office currency is supported (a visitor, or an online order). The line
  stores the converted amount and the rate used at purchase time.
* Per-currency subtotals are never added together. The grand total converts each office
  subtotal once, at a persisted month-end rate snapshot (source and date shown), and
  discloses any currency it could not convert.
* HR reimburses in the employee's payroll currency at HR's own rate; that is recorded on the
  claim, not invented by us.

## 5. Trade-offs and what I deliberately did not build

| Decision | Cost | Why |
|---|---|---|
| Offer-to-accept, not silent assignment | refill can take up to 45 min longer | silence is the actual bug today; an unanswered offer is visible |
| SQLite + single process | no horizontal scaling, no HA | 35 users and <100 claims/month; a queue would be theatre |
| Presence is declared (roster + daily opt-in), not badge/CCTV inferred | one tap per day | privacy and works with hybrid work |
| Expected cost is a median of past receipts | estimates can be off | the ledger releases the estimate and books the actual, so drift never becomes spend |
| 6-hour re-report guard | a genuinely fast-emptying box needs a manager | prevents the exact "two people shopped" failure |
| Manual manager override for over-budget | a human in the loop | blocking snacks on a spreadsheet is a policy call, not a code call |

**Not built:** payments or corporate cards; HR approval logic; inventory forecasting or
vendor ordering; barcode/IoT weight sensors/computer vision; ML for choosing buyers;
multi-tenancy; a real Slack app (the adapter is real, the client is simulated); event
sourcing or a message bus; a separate accounting ledger.

## 6. Assumptions to confirm before production

Reimbursement is out-of-pocket (no card/petty cash); payroll currency equals home-office
currency; the chat tool supports interactive buttons; HR accepts an idempotency key (we
deduplicate locally regardless); one receipt may cover several boxes; finance wants NOK as
the reporting currency and month-end rates; and over-budget refills need manager approval
rather than a hard stop.
