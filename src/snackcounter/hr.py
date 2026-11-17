from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol

from .clock import Clock, parse_iso, utc_iso
from .db import one
from .errors import HrUnavailable
from .money import Currency, Money, convert


@dataclass(frozen=True)
class HrClaimResult:
    hr_claim_id: str
    status: str
    expected_decision_at: datetime | None
    amount_minor: int
    currency: str
    reimbursed_amount_minor: int | None = None
    reimbursed_currency: str | None = None
    reject_reason: str | None = None


class HrPort(Protocol):
    def submit_claim(
        self,
        *,
        employee_id: str,
        amount_minor: int,
        currency: str,
        description: str,
        receipt_ref: str,
        idempotency_key: str,
        submitted_at: datetime,
    ) -> HrClaimResult: ...

    def get_claim(self, hr_claim_id: str) -> HrClaimResult: ...


class FakeHr:
    """Simulated HR system: separate tables, its own FX rate, approvals in 1-10 days.

    Idempotency is keyed on the caller's idempotency key, which is what makes
    outbox retries safe: resubmitting the same claim never creates a second one.
    """

    # HR is a different company and does the payout conversion itself.
    PAYOUT_RATES = {"NOK>INR": Decimal("8.30"), "INR>NOK": Decimal("0.12048")}

    def __init__(
        self,
        db,
        clock: Clock,
        *,
        unavailable: bool = False,
        reject_keys: set[str] | None = None,
    ) -> None:
        self.db = db
        self.clock = clock
        self.unavailable = unavailable
        self.reject_keys = reject_keys or set()

    # -- internal ---------------------------------------------------------
    def _row_to_result(self, row) -> HrClaimResult:
        return HrClaimResult(
            hr_claim_id=row["hr_claim_id"],
            status=row["status"],
            expected_decision_at=parse_iso(row["decision_at"]),
            amount_minor=row["amount_minor"],
            currency=row["currency"],
            reimbursed_amount_minor=row["reimbursed_amount_minor"],
            reimbursed_currency=row["reimbursed_currency"],
            reject_reason=row["reject_reason"],
        )

    def _payroll_currency(self, employee_id: str) -> str:
        row = one(
            self.db.conn,
            "SELECT payroll_currency FROM employees WHERE hr_employee_id = ?",
            (employee_id,),
        )
        if row is None:
            raise HrUnavailable(f"unknown employee {employee_id}")
        return row["payroll_currency"]

    def _decide(self, row, now: datetime) -> None:
        if row["status"] != "PENDING":
            return
        rejected = row["idempotency_key"] in self.reject_keys
        payroll = row["payroll_currency"]
        amount = Money(row["amount_minor"], Currency(row["currency"]))
        pair = f"{amount.currency.value}>{payroll}"
        rate = self.PAYOUT_RATES.get(pair, Decimal("1"))
        payout = convert(amount, payroll, rate)
        with self.db.tx():
            self.db.conn.execute(
                "UPDATE hr_claims SET status = ?, reimbursed_amount_minor = ?,"
                " reimbursed_currency = ?, reject_reason = ? WHERE hr_claim_id = ?",
                (
                    "REJECTED" if rejected else "APPROVED",
                    payout.minor,
                    payout.currency.value,
                    "Receipt unreadable" if rejected else None,
                    row["hr_claim_id"],
                ),
            )

    # -- port -------------------------------------------------------------
    def submit_claim(
        self,
        *,
        employee_id: str,
        amount_minor: int,
        currency: str,
        description: str,
        receipt_ref: str,
        idempotency_key: str,
        submitted_at: datetime,
    ) -> HrClaimResult:
        if self.unavailable:
            raise HrUnavailable("HR system timeout while creating claim")

        payroll = self._payroll_currency(employee_id)
        with self.db.tx():
            existing = one(
                self.db.conn,
                "SELECT * FROM hr_claims WHERE idempotency_key = ?",
                (idempotency_key,),
            )
            if existing is not None:
                return self._row_to_result(existing)

            digest = hashlib.sha1(idempotency_key.encode()).hexdigest()
            delay_days = 1 + int(digest[:8], 16) % 10
            decision_at = submitted_at + timedelta(days=delay_days)
            hr_claim_id = "HR-" + digest[:10].upper()
            self.db.conn.execute(
                "INSERT INTO hr_claims (hr_claim_id, idempotency_key, employee_id, amount_minor,"
                " currency, description, receipt_ref, submitted_at, decision_at, status,"
                " payroll_currency) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?)",
                (
                    hr_claim_id,
                    idempotency_key,
                    employee_id,
                    amount_minor,
                    currency,
                    description,
                    receipt_ref,
                    utc_iso(submitted_at),
                    utc_iso(decision_at),
                    payroll,
                ),
            )
            row = one(self.db.conn, "SELECT * FROM hr_claims WHERE hr_claim_id = ?", (hr_claim_id,))
            return self._row_to_result(row)

    def get_claim(self, hr_claim_id: str) -> HrClaimResult:
        if self.unavailable:
            raise HrUnavailable("HR system timeout while reading claim")
        row = one(self.db.conn, "SELECT * FROM hr_claims WHERE hr_claim_id = ?", (hr_claim_id,))
        if row is None:
            raise HrUnavailable(f"unknown HR claim {hr_claim_id}")
        if row["status"] == "PENDING" and self.clock.now() >= parse_iso(row["decision_at"]):
            self._decide(row, self.clock.now())
            row = one(self.db.conn, "SELECT * FROM hr_claims WHERE hr_claim_id = ?", (hr_claim_id,))
        return self._row_to_result(row)


class UnavailableHr:
    """Used by tests to prove the outbox never loses a claim while HR is down."""

    def __init__(self, error: str = "HR system down") -> None:
        self.error = error

    def submit_claim(self, **kwargs) -> HrClaimResult:
        raise HrUnavailable(self.error)

    def get_claim(self, hr_claim_id: str) -> HrClaimResult:
        raise HrUnavailable(self.error)
