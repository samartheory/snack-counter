"""Refill lifecycle, fair assignment, budget guardrails and the sweep loop.

Design rules enforced here:
  * every state change is a conditional UPDATE (compare-and-swap), never a
    read-then-write;
  * the database, not the application, is the arbiter of "one active refill";
  * no money is ever added across currencies.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
import uuid
from datetime import datetime, timedelta

from .chat import ChatPort, FakeChat
from .clock import (
    Clock,
    SystemClock,
    fmt_local,
    local_date,
    next_business_deadline,
    parse_holidays,
    parse_iso,
    utc_iso,
)
from .config import Settings
from .db import Database, many, one, scalar
from .errors import Conflict, NotFound, ValidationError
from .fx import lookup_rate
from .hr import FakeHr, HrPort
from .money import Currency, Money, convert
from .policy import is_eligible_now, rank_candidates

ACTIVE_STATUSES = ("OPEN", "OFFERED", "BROADCAST", "ASSIGNED")
OPEN_ACCEPT_ACTIONS = [
    {"id": "accept", "label": "I'll buy it"},
    {"id": "decline", "label": "Can't today"},
]
ASSIGNED_ACTIONS = [
    {"id": "purchase", "label": "Bought it — log receipt"},
    {"id": "still_on_it", "label": "Still on it"},
]


class SnackService:
    def __init__(
        self,
        db: Database,
        clock: Clock | None = None,
        chat: ChatPort | None = None,
        hr: HrPort | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.db = db
        self.clock = clock or SystemClock()
        self.settings = settings or Settings()
        self.chat: ChatPort = chat or FakeChat(db, self.clock)
        self.hr: HrPort = hr or FakeHr(db, self.clock)

    # ------------------------------------------------------------------ ids
    def _id(self, prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex[:10]}"

    # --------------------------------------------------------------- lookups
    def _office(self, conn, office_id: str) -> dict:
        row = one(conn, "SELECT * FROM offices WHERE id = ?", (office_id,))
        if row is None:
            raise NotFound(f"office {office_id} not found")
        return row

    def _employee(self, conn, employee_id: str) -> dict:
        row = one(conn, "SELECT * FROM employees WHERE id = ?", (employee_id,))
        if row is None:
            raise NotFound(f"employee {employee_id} not found")
        return row

    def _box(self, conn, box_id: str) -> dict:
        row = one(conn, "SELECT * FROM boxes WHERE id = ?", (box_id,))
        if row is None:
            raise NotFound(f"box {box_id} not found")
        return row

    def _raw_refill(self, conn, refill_id: str) -> dict:
        row = one(conn, "SELECT * FROM refills WHERE id = ?", (refill_id,))
        if row is None:
            raise NotFound(f"refill {refill_id} not found")
        return row

    def refill_view(self, refill_id: str) -> dict:
        row = one(
            self.db.conn,
            """
            SELECT r.*, b.label AS box_label, b.category AS box_category, b.status AS box_status,
                   o.name AS office_name, o.currency AS office_currency, o.timezone AS office_timezone,
                   e.name AS assignee_name, ot.name AS offered_to_name,
                   p.receipt_amount_minor, p.receipt_currency, p.receipt_ref,
                   c.status AS claim_status, c.hr_claim_id, c.decided_at AS claim_decided_at,
                   c.reimbursed_amount_minor, c.reimbursed_currency, c.reject_reason
              FROM refills r
              JOIN boxes b ON b.id = r.box_id
              JOIN offices o ON o.id = r.office_id
              LEFT JOIN employees e ON e.id = r.assignee_id
              LEFT JOIN employees ot ON ot.id = r.offered_to_id
              LEFT JOIN purchases p ON p.id = r.purchase_id
              LEFT JOIN claims c ON c.purchase_id = r.purchase_id
             WHERE r.id = ?
            """,
            (refill_id,),
        )
        if row is None:
            raise NotFound(f"refill {refill_id} not found")
        return row

    def list_refills(self, limit: int = 40) -> list[dict]:
        rows = many(
            self.db.conn,
            "SELECT id FROM refills ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (limit,),
        )
        return [self.refill_view(r["id"]) for r in rows]

    # ----------------------------------------------------------------- money
    def _period(self, office: dict, when: datetime) -> str:
        return local_date(when, office["timezone"]).strftime("%Y-%m")

    def _rate(self, conn, from_currency: str, to_currency: str, as_of: str) -> tuple[str, str]:
        """Return (rate_text, source). The rate is stored with the expense, not recomputed later."""
        return lookup_rate(conn, from_currency, to_currency, as_of)

    def _expected_cost(self, conn, office: dict) -> int:
        rows = conn.execute(
            "SELECT amount_office_minor FROM purchase_lines WHERE office_id = ?"
            " ORDER BY purchased_on DESC LIMIT 10",
            (office["id"],),
        ).fetchall()
        if rows:
            estimate = int(round(statistics.median([r[0] for r in rows])))
        else:
            estimate = (office["typical_min_minor"] + office["typical_max_minor"]) // 2
        return max(office["typical_min_minor"], min(office["typical_max_minor"], estimate))

    def budget_status(self, office_id: str, period: str | None = None) -> dict:
        conn = self.db.conn
        office = self._office(conn, office_id)
        period = period or self._period(office, self.clock.now())
        totals = one(
            conn,
            """
            SELECT COALESCE(SUM(CASE WHEN kind = 'reserve' THEN amount_minor
                                     WHEN kind = 'release' THEN -amount_minor ELSE 0 END), 0) AS reserved,
                   COALESCE(SUM(CASE WHEN kind = 'spend' THEN amount_minor ELSE 0 END), 0) AS spent
              FROM budget_entries WHERE office_id = ? AND period = ?
            """,
            (office_id, period),
        )
        override = one(
            conn, "SELECT * FROM budget_overrides WHERE office_id = ? AND period = ?", (office_id, period)
        )
        budget = office["monthly_budget_minor"]
        reserved = int(totals["reserved"] or 0)
        spent = int(totals["spent"] or 0)
        return {
            "office_id": office_id,
            "office_name": office["name"],
            "period": period,
            "currency": office["currency"],
            "budget_minor": budget,
            "spent_minor": spent,
            "reserved_minor": reserved,
            "available_minor": budget - spent - reserved,
            "overridden": override is not None,
        }

    def _reserve(self, conn, refill: dict, office: dict, now: datetime) -> bool:
        period = self._period(office, now)
        status = self.budget_status(office["id"], period)
        if status["available_minor"] < refill["expected_cost_minor"] and not status["overridden"]:
            conn.execute(
                "UPDATE refills SET blocked_reason = 'BUDGET' WHERE id = ?", (refill["id"],)
            )
            self._notify(
                conn,
                f"#{office['name'].lower()}-snacks",
                f":warning: {office['name']} snack budget is exhausted for {period} "
                f"({Money(status['spent_minor'], office['currency'])} spent, "
                f"{Money(status['reserved_minor'], office['currency'])} reserved of "
                f"{Money(status['budget_minor'], office['currency'])}). Refill is waiting for a manager override.",
                kind="alert",
                refill_id=refill["id"],
                employee_id=office["manager_employee_id"],
            )
            self._audit(conn, None, "refill", refill["id"], "budget_blocked", {"period": period})
            return False

        entry_id = self._id("be")
        conn.execute(
            "INSERT INTO budget_entries (id, office_id, period, kind, amount_minor, currency,"
            " refill_id, created_at, created_by) VALUES (?, ?, ?, 'reserve', ?, ?, ?, ?, 'system')",
            (
                entry_id,
                office["id"],
                period,
                refill["expected_cost_minor"],
                office["currency"],
                refill["id"],
                utc_iso(now),
            ),
        )
        conn.execute(
            "UPDATE refills SET reserved_minor = ?, reserve_entry_id = ? WHERE id = ?",
            (refill["expected_cost_minor"], entry_id, refill["id"]),
        )
        return True

    def _release(self, conn, refill: dict, now: datetime, actor: str) -> None:
        if not refill["reserved_minor"]:
            return
        reserve_entry = one(
            conn, "SELECT * FROM budget_entries WHERE id = ?", (refill["reserve_entry_id"],)
        )
        period = reserve_entry["period"] if reserve_entry else self._period(
            self._office(conn, refill["office_id"]), now
        )
        conn.execute(
            "INSERT INTO budget_entries (id, office_id, period, kind, amount_minor, currency,"
            " refill_id, created_at, created_by) VALUES (?, ?, ?, 'release', ?, ?, ?, ?, ?)",
            (
                self._id("be"),
                refill["office_id"],
                period,
                refill["reserved_minor"],
                refill["currency"],
                refill["id"],
                utc_iso(now),
                actor,
            ),
        )
        conn.execute(
            "UPDATE refills SET reserved_minor = 0, reserve_entry_id = NULL WHERE id = ?",
            (refill["id"],),
        )

    # ----------------------------------------------------------- transitions
    def _transition(
        self,
        conn,
        refill_id: str,
        from_statuses: tuple[str, ...],
        expected_version: int,
        **fields,
    ) -> None:
        """The single choke point. Row count 0 means somebody else got there first."""
        if not fields:
            raise ValueError("transition needs at least one field")
        assignments = ", ".join(f"{name} = ?" for name in fields)
        marks = ", ".join("?" for _ in from_statuses)
        sql = (
            f"UPDATE refills SET {assignments}, version = version + 1 "
            f"WHERE id = ? AND version = ? AND status IN ({marks})"
        )
        cursor = conn.execute(sql, [*fields.values(), refill_id, expected_version, *from_statuses])
        if cursor.rowcount != 1:
            current = one(
                conn,
                "SELECT r.status, e.name AS assignee FROM refills r"
                " LEFT JOIN employees e ON e.id = r.assignee_id WHERE r.id = ?",
                (refill_id,),
            )
            holder = current["assignee"] if current and current["assignee"] else "another volunteer"
            raise Conflict(
                f"refill is no longer available (status: {current['status'] if current else 'unknown'},"
                f" held by {holder})",
                status=current["status"] if current else None,
            )

    def _audit(self, conn, actor_id, entity, entity_id, action, detail=None) -> None:
        conn.execute(
            "INSERT INTO audit (id, at, actor_id, entity, entity_id, action, detail)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                self._id("au"),
                utc_iso(self.clock.now()),
                actor_id,
                entity,
                entity_id,
                action,
                json.dumps(detail or {}),
            ),
        )

    def _notify(
        self,
        conn,
        channel: str,
        text: str,
        *,
        kind: str = "info",
        refill_id: str | None = None,
        employee_id: str | None = None,
        actions: list[dict] | None = None,
    ) -> None:
        self.chat.send(
            channel,
            text,
            kind=kind,
            refill_id=refill_id,
            employee_id=employee_id,
            actions=actions,
        )

    def _channel(self, office: dict) -> str:
        return f"#{office['name'].lower().replace(' ', '-')}-snacks"

    def _due_at(self, office: dict, now: datetime) -> datetime:
        return next_business_deadline(
            now,
            office["timezone"],
            parse_holidays(office["holidays"]),
            due_hour=self.settings.due_hour_local,
            business_days=self.settings.purchase_due_business_days,
        )

    # ---------------------------------------------------------------- public
    def report_empty(self, box_id: str, actor_id: str, note: str | None = None) -> dict:
        """A box is empty. Creates the coordination record and reserves budget."""
        now = self.clock.now()
        with self.db.tx():
            conn = self.db.conn
            box = self._box(conn, box_id)
            self._employee(conn, actor_id)
            office = self._office(conn, box["office_id"])

            existing = one(
                conn,
                "SELECT * FROM refills WHERE box_id = ? AND status IN ('OPEN','OFFERED','BROADCAST','ASSIGNED')",
                (box_id,),
            )
            if existing is not None:
                holder = existing["assignee_id"] or existing["offered_to_id"]
                name = one(conn, "SELECT name FROM employees WHERE id = ?", (holder,)) if holder else None
                raise Conflict(
                    f"this box is already being handled by {name['name'] if name else 'someone'}",
                    refill_id=existing["id"],
                    status=existing["status"],
                )

            recent = one(
                conn,
                """
                SELECT r.id, p.purchased_at FROM refills r
                  JOIN purchases p ON p.id = r.purchase_id
                 WHERE r.box_id = ? AND r.status IN ('PURCHASED', 'REIMBURSED')
                   AND p.purchased_at >= ?
                 ORDER BY p.purchased_at DESC LIMIT 1
                """,
                (box_id, utc_iso(now - timedelta(hours=self.settings.repeat_purchase_guard_hours))),
            )
            if recent is not None:
                raise Conflict(
                    f"this box was already refilled at "
                    f"{fmt_local(parse_iso(recent['purchased_at']), office['timezone'])}. "
                    f"Ask a manager if it is genuinely empty again.",
                    refill_id=recent["id"],
                    status="PURCHASED",
                )

            refill_id = self._id("rf")
            try:
                conn.execute(
                    "INSERT INTO refills (id, box_id, office_id, status, version, created_at,"
                    " expected_cost_minor, currency, priority) VALUES (?, ?, ?, 'OPEN', 0, ?, ?, ?, ?)",
                    (
                        refill_id,
                        box_id,
                        office["id"],
                        utc_iso(now),
                        self._expected_cost(conn, office),
                        office["currency"],
                        "urgent" if note and "urgent" in note.lower() else "normal",
                    ),
                )
            except sqlite3.IntegrityError as exc:  # defensive: the unique index is the backstop
                raise Conflict("this box already has an active refill") from exc

            conn.execute("UPDATE boxes SET status = 'empty' WHERE id = ?", (box_id,))
            self._audit(conn, actor_id, "refill", refill_id, "reported_empty", {"note": note})
            self._notify(
                conn,
                self._channel(office),
                f"{box['label']} at {office['name']} is empty — reported by "
                f"{self._employee(conn, actor_id)['name']}.",
                refill_id=refill_id,
                kind="empty",
            )
            self._reserve_safely(conn, refill_id, office, now)
        return self.refill_view(refill_id)

    def _reserve_safely(self, conn, refill_id: str, office: dict, now: datetime) -> None:
        refill = self._raw_refill(conn, refill_id)
        if self._reserve(conn, refill, office, now):
            self._offer_next(conn, refill_id)

    # -- assignment -------------------------------------------------------
    def _offer_next(self, conn, refill_id: str) -> None:
        refill = self._raw_refill(conn, refill_id)
        if refill["blocked_reason"]:
            return
        now = self.clock.now()
        office = self._office(conn, refill["office_id"])
        box = self._box(conn, refill["box_id"])
        on_date = local_date(now, office["timezone"]).isoformat()
        exclusions = {
            row["employee_id"]
            for row in conn.execute(
                "SELECT employee_id FROM refill_exclusions WHERE refill_id = ?", (refill_id,)
            ).fetchall()
        }
        attempts = int(
            scalar(conn, "SELECT COUNT(*) FROM offers WHERE refill_id = ?", (refill_id,)) or 0
        )
        ranked = rank_candidates(
            conn,
            office_id=office["id"],
            refill_id=refill_id,
            on_date=on_date,
            now=now,
            exclude_ids=exclusions,
            settings=self.settings,
        )
        channel = self._channel(office)

        if ranked and attempts < self.settings.offers_before_broadcast:
            candidate = ranked[0]
            expires = now + timedelta(minutes=self.settings.offer_window_minutes)
            self._transition(
                conn,
                refill_id,
                ("OPEN", "OFFERED"),
                refill["version"],
                status="OFFERED",
                offered_to_id=candidate["id"],
                offered_at=utc_iso(now),
                offer_expires_at=utc_iso(expires),
                blocked_reason=None,
            )
            conn.execute(
                "UPDATE offers SET status = 'superseded' WHERE refill_id = ? AND status = 'offered'",
                (refill_id,),
            )
            conn.execute(
                "INSERT INTO offers (id, refill_id, employee_id, rank, status, created_at, expires_at)"
                " VALUES (?, ?, ?, ?, 'offered', ?, ?)",
                (
                    self._id("of"),
                    refill_id,
                    candidate["id"],
                    attempts,
                    utc_iso(now),
                    utc_iso(expires),
                ),
            )
            self._notify(
                conn,
                channel,
                f"{candidate['name']}, {box['label']} at {office['name']} is empty. "
                f"Can you refill it? Expected spend ~"
                f"{Money(refill['expected_cost_minor'], refill['currency'])}. "
                f"Take it within {self.settings.offer_window_minutes} minutes.",
                refill_id=refill_id,
                employee_id=candidate["id"],
                kind="offer",
                actions=OPEN_ACCEPT_ACTIONS,
            )
            self._audit(conn, None, "refill", refill_id, "offered", {"employee_id": candidate["id"]})
            return

        # No taker for the sequence, or too many declines: open it to the room.
        expires = now + timedelta(minutes=self.settings.offer_window_minutes)
        self._transition(
            conn,
            refill_id,
            ("OPEN", "OFFERED", "BROADCAST"),
            refill["version"],
            status="BROADCAST",
            offered_to_id=None,
            offered_at=utc_iso(now),
            offer_expires_at=utc_iso(expires),
            blocked_reason=None,
        )
        conn.execute(
            "UPDATE offers SET status = 'superseded' WHERE refill_id = ? AND status = 'offered'",
            (refill_id,),
        )
        self._notify(
            conn,
            channel,
            f"No one has picked up {box['label']} at {office['name']}. "
            f"Anyone on site can take it — first to accept wins. "
            f"Expected spend ~{Money(refill['expected_cost_minor'], refill['currency'])}.",
            refill_id=refill_id,
            kind="broadcast",
            actions=[{"id": "accept", "label": "I'll take it"}],
        )
        self._audit(conn, None, "refill", refill_id, "broadcast", {})

    def accept(self, refill_id: str, employee_id: str) -> dict:
        """Commit to a refill. This is the race the database has to settle."""
        now = self.clock.now()
        with self.db.tx():
            conn = self.db.conn
            refill = self._raw_refill(conn, refill_id)
            employee = self._employee(conn, employee_id)
            office = self._office(conn, refill["office_id"])

            if refill["status"] == "OFFERED":
                if refill["offered_to_id"] != employee_id:
                    raise Conflict("this refill was offered to someone else", refill_id=refill_id)
                if refill["offer_expires_at"] and now >= parse_iso(refill["offer_expires_at"]):
                    raise Conflict("the offer has expired", refill_id=refill_id)
            elif refill["status"] == "BROADCAST":
                on_date = local_date(now, office["timezone"]).isoformat()
                if not is_eligible_now(
                    conn,
                    refill=refill,
                    employee_id=employee_id,
                    on_date=on_date,
                    settings=self.settings,
                ):
                    raise Conflict(
                        "you are not eligible right now (not on site, or already holding a refill/claims)",
                        refill_id=refill_id,
                    )
            else:
                holder = self._raw_refill(conn, refill_id)["assignee_id"]
                name = one(conn, "SELECT name FROM employees WHERE id = ?", (holder,)) if holder else None
                raise Conflict(
                    f"refill is {refill['status'].lower()}"
                    + (f" and held by {name['name']}" if name else ""),
                    refill_id=refill_id,
                    status=refill["status"],
                )

            due = self._due_at(office, now)
            self._transition(
                conn,
                refill_id,
                ("OFFERED", "BROADCAST"),
                refill["version"],
                status="ASSIGNED",
                assignee_id=employee_id,
                accepted_at=utc_iso(now),
                due_at=utc_iso(due),
                offered_to_id=None,
                offer_expires_at=None,
                blocked_reason=None,
            )
            conn.execute(
                "UPDATE offers SET status = 'superseded', responded_at = ?"
                " WHERE refill_id = ? AND status = 'offered' AND employee_id != ?",
                (utc_iso(now), refill_id, employee_id),
            )
            conn.execute(
                "INSERT OR IGNORE INTO offers"
                " (id, refill_id, employee_id, rank, status, created_at, expires_at)"
                " VALUES (?, ?, ?, 0, 'accepted', ?, ?)",
                (self._id("of"), refill_id, employee_id, utc_iso(now), utc_iso(now)),
            )
            conn.execute(
                "UPDATE offers SET status = 'accepted', responded_at = ?"
                " WHERE refill_id = ? AND employee_id = ?",
                (utc_iso(now), refill_id, employee_id),
            )
            box = self._box(conn, refill["box_id"])
            self._notify(
                conn,
                self._channel(office),
                f"{employee['name']} is on it: {box['label']}. "
                f"Due {fmt_local(due, office['timezone'])}.",
                refill_id=refill_id,
                employee_id=employee_id,
                kind="assigned",
                actions=ASSIGNED_ACTIONS,
            )
            self._audit(conn, employee_id, "refill", refill_id, "accepted", {"due_at": utc_iso(due)})
        return self.refill_view(refill_id)

    def decline(self, refill_id: str, employee_id: str, reason: str | None = None) -> dict:
        now = self.clock.now()
        with self.db.tx():
            conn = self.db.conn
            refill = self._raw_refill(conn, refill_id)
            if refill["status"] not in ("OFFERED", "BROADCAST"):
                raise Conflict(f"refill is {refill['status'].lower()}, cannot decline")
            if refill["status"] == "OFFERED" and refill["offered_to_id"] != employee_id:
                raise Conflict("this refill was offered to someone else")
            conn.execute(
                "UPDATE offers SET status = 'declined', responded_at = ?"
                " WHERE refill_id = ? AND employee_id = ?",
                (utc_iso(now), refill_id, employee_id),
            )
            conn.execute(
                "INSERT OR IGNORE INTO refill_exclusions (refill_id, employee_id, reason, created_at)"
                " VALUES (?, ?, 'declined', ?)",
                (refill_id, employee_id, utc_iso(now)),
            )
            if refill["status"] == "OFFERED":
                self._transition(
                    conn,
                    refill_id,
                    ("OFFERED",),
                    refill["version"],
                    status="OPEN",
                    offered_to_id=None,
                    offer_expires_at=None,
                )
                self._audit(conn, employee_id, "refill", refill_id, "declined", {"reason": reason})
                self._offer_next(conn, refill_id)
        return self.refill_view(refill_id)

    def mark_still_on_it(self, refill_id: str, employee_id: str) -> dict:
        now = self.clock.now()
        with self.db.tx():
            conn = self.db.conn
            refill = self._raw_refill(conn, refill_id)
            if refill["status"] != "ASSIGNED" or refill["assignee_id"] != employee_id:
                raise Conflict("only the current assignee can extend this refill")
            if refill["extension_count"] >= 2:
                raise Conflict("already extended twice — please log the receipt or ask for a handover")
            office = self._office(conn, refill["office_id"])
            base = max(now, parse_iso(refill["due_at"])) if refill["due_at"] else now
            new_due = self._due_at(office, base)
            self._transition(
                conn,
                refill_id,
                ("ASSIGNED",),
                refill["version"],
                due_at=utc_iso(new_due),
                extension_count=refill["extension_count"] + 1,
                reminders_sent=0,
            )
            self._notify(
                conn,
                self._channel(office),
                f"{self._employee(conn, employee_id)['name']} is still on it. "
                f"New deadline {fmt_local(new_due, office['timezone'])}.",
                refill_id=refill_id,
                employee_id=employee_id,
            )
            self._audit(conn, employee_id, "refill", refill_id, "extended", {"due_at": utc_iso(new_due)})
        return self.refill_view(refill_id)

    def cancel(self, refill_id: str, actor_id: str, reason: str) -> dict:
        now = self.clock.now()
        with self.db.tx():
            conn = self.db.conn
            refill = self._raw_refill(conn, refill_id)
            if refill["status"] not in ACTIVE_STATUSES:
                raise Conflict(f"refill is {refill['status'].lower()}, cannot cancel")
            self._release(conn, refill, now, actor_id)
            self._transition(
                conn,
                refill_id,
                ACTIVE_STATUSES,
                refill["version"],
                status="CANCELLED",
                cancel_reason=reason,
                assignee_id=None,
                offered_to_id=None,
                offer_expires_at=None,
                blocked_reason=None,
            )
            office = self._office(conn, refill["office_id"])
            self._notify(
                conn,
                self._channel(office),
                f"Refill cancelled: {reason}.",
                refill_id=refill_id,
                kind="cancelled",
            )
            self._audit(conn, actor_id, "refill", refill_id, "cancelled", {"reason": reason})
        return self.refill_view(refill_id)

    def reopen(self, refill_id: str, actor_id: str) -> dict:
        now = self.clock.now()
        with self.db.tx():
            conn = self.db.conn
            refill = self._raw_refill(conn, refill_id)
            if refill["status"] != "OPEN":
                raise Conflict(f"refill is {refill['status'].lower()}, cannot reopen")
            conn.execute(
                "UPDATE refills SET blocked_reason = NULL, version = version + 1 WHERE id = ?",
                (refill_id,),
            )
            self._audit(conn, actor_id, "refill", refill_id, "reopened", {})
            fresh = self._raw_refill(conn, refill_id)
            office = self._office(conn, refill["office_id"])
            if not fresh["reserved_minor"]:
                if not self._reserve(conn, fresh, office, now):
                    return self.refill_view(refill_id)
            self._offer_next(conn, refill_id)
        return self.refill_view(refill_id)

    # -- purchase / claim -------------------------------------------------
    def record_purchase(
        self,
        buyer_id: str,
        lines: list[dict],
        currency: str,
        receipt_ref: str,
        purchased_at: datetime | None = None,
        allow_rescue: bool = False,
    ) -> dict:
        """One trip, one receipt, one HR claim. Lines allocate the receipt across boxes."""
        if not lines:
            raise ValidationError("at least one refill line is required")
        if not receipt_ref:
            raise ValidationError("a receipt image is required by the HR system")
        currency = Currency(currency).value
        now = purchased_at or self.clock.now()
        receipt_total = sum(int(line["amount_minor"]) for line in lines)
        refill_ids = [line["refill_id"] for line in lines]
        if len(set(refill_ids)) != len(refill_ids):
            raise ValidationError("the same refill cannot appear twice on one receipt")
        if receipt_total <= 0:
            raise ValidationError("receipt total must be positive")

        with self.db.tx():
            conn = self.db.conn
            buyer = self._employee(conn, buyer_id)
            refills = []
            for line in lines:
                if int(line["amount_minor"]) <= 0:
                    raise ValidationError("each allocation must be positive")
                refill = self._raw_refill(conn, line["refill_id"])
                if refill["status"] == "ASSIGNED":
                    if refill["assignee_id"] != buyer_id:
                        raise Conflict("this refill belongs to another volunteer")
                elif refill["status"] in ("OPEN", "OFFERED", "BROADCAST") and allow_rescue:
                    # Someone is at the shop anyway and picks up an unclaimed box.
                    conn.execute(
                        "UPDATE offers SET status = 'superseded' WHERE refill_id = ? AND status = 'offered'",
                        (refill["id"],),
                    )
                else:
                    raise Conflict(
                        f"refill is {refill['status'].lower()} and cannot be purchased",
                        refill_id=refill["id"],
                    )
                refills.append(refill)

            purchase_id = self._id("pu")
            conn.execute(
                "INSERT INTO purchases (id, buyer_id, purchased_at, receipt_amount_minor,"
                " receipt_currency, receipt_ref, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    purchase_id,
                    buyer_id,
                    utc_iso(now),
                    receipt_total,
                    currency,
                    receipt_ref,
                    utc_iso(self.clock.now()),
                ),
            )

            labels = []
            for refill, line in zip(refills, lines):
                amount = int(line["amount_minor"])
                office = self._office(conn, refill["office_id"])
                purchased_on = local_date(now, office["timezone"])
                rate_text, _source = self._rate(
                    conn, currency, office["currency"], purchased_on.isoformat()
                )
                amount_office = convert(
                    Money(amount, currency), office["currency"], rate_text
                ).minor
                period = purchased_on.strftime("%Y-%m")
                conn.execute(
                    "INSERT INTO purchase_lines (purchase_id, refill_id, office_id, currency,"
                    " amount_minor, office_currency, amount_office_minor, fx_rate, purchased_on, period)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        purchase_id,
                        refill["id"],
                        office["id"],
                        currency,
                        amount,
                        office["currency"],
                        amount_office,
                        rate_text,
                        purchased_on.isoformat(),
                        period,
                    ),
                )
                self._release(conn, refill, now, buyer_id)
                conn.execute(
                    "INSERT INTO budget_entries (id, office_id, period, kind, amount_minor, currency,"
                    " refill_id, purchase_id, source_amount_minor, source_currency, fx_rate,"
                    " created_at, created_by) VALUES (?, ?, ?, 'spend', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        self._id("be"),
                        office["id"],
                        period,
                        amount_office,
                        office["currency"],
                        refill["id"],
                        purchase_id,
                        amount,
                        currency,
                        rate_text,
                        utc_iso(now),
                        buyer_id,
                    ),
                )
                self._transition(
                    conn,
                    refill["id"],
                    ("ASSIGNED", "OPEN", "OFFERED", "BROADCAST"),
                    refill["version"],
                    status="PURCHASED",
                    assignee_id=buyer_id,
                    purchased_at=utc_iso(now),
                    purchase_id=purchase_id,
                    offered_to_id=None,
                    offer_expires_at=None,
                    blocked_reason=None,
                )
                conn.execute("UPDATE boxes SET status = 'stocked' WHERE id = ?", (refill["box_id"],))
                box = self._box(conn, refill["box_id"])
                labels.append(f"{box['label']} ({office['name']})")

            claim_id = self._id("cl")
            conn.execute(
                "INSERT INTO claims (id, purchase_id, employee_id, amount_minor, currency, status,"
                " submitted_at) VALUES (?, ?, ?, ?, ?, 'SUBMITTING', ?)",
                (claim_id, purchase_id, buyer_id, receipt_total, currency, utc_iso(self.clock.now())),
            )
            description = "Snacks: " + ", ".join(labels)
            payload = {
                "claim_id": claim_id,
                "purchase_id": purchase_id,
                "hr_employee_id": buyer["hr_employee_id"],
                "amount_minor": receipt_total,
                "currency": currency,
                "description": description,
                "receipt_ref": receipt_ref,
                "submitted_at": utc_iso(self.clock.now()),
            }
            conn.execute(
                "INSERT INTO outbox (id, kind, payload, created_at) VALUES (?, 'hr.submit_claim', ?, ?)",
                (self._id("ob"), json.dumps(payload), utc_iso(self.clock.now())),
            )
            self._audit(
                conn,
                buyer_id,
                "purchase",
                purchase_id,
                "recorded",
                {"refills": refill_ids, "amount_minor": receipt_total, "currency": currency},
            )
            self._notify(
                conn,
                self._channel(self._office(conn, refills[0]["office_id"])),
                f"{buyer['name']} bought snacks for {Money(receipt_total, currency)} "
                f"across {len(refills)} box(es). Claim is on its way to HR.",
                refill_id=refills[0]["id"],
                kind="purchased",
            )
            primary = refills[0]["id"]

        self.deliver_outbox()
        return self.refill_view(primary)

    def deliver_outbox(self) -> int:
        """At-least-once delivery, exactly-once effect (HR dedupes on the idempotency key)."""
        sent = 0
        events = many(
            self.db.conn,
            "SELECT * FROM outbox WHERE processed_at IS NULL ORDER BY created_at LIMIT 20",
        )
        for event in events:
            payload = json.loads(event["payload"])
            try:
                result = self.hr.submit_claim(
                    employee_id=payload["hr_employee_id"],
                    amount_minor=payload["amount_minor"],
                    currency=payload["currency"],
                    description=payload["description"],
                    receipt_ref=payload["receipt_ref"],
                    idempotency_key=payload["purchase_id"],
                    submitted_at=parse_iso(payload["submitted_at"]),
                )
            except Exception as exc:  # HR down: keep the event, try again later
                with self.db.tx():
                    conn = self.db.conn
                    conn.execute(
                        "UPDATE outbox SET attempts = attempts + 1, last_error = ? WHERE id = ?",
                        (str(exc), event["id"]),
                    )
                    conn.execute(
                        "UPDATE claims SET attempts = attempts + 1 WHERE id = ?",
                        (payload["claim_id"],),
                    )
                continue

            now = self.clock.now()
            with self.db.tx():
                conn = self.db.conn
                claim = one(conn, "SELECT * FROM claims WHERE id = ?", (payload["claim_id"],))
                conn.execute(
                    "UPDATE claims SET hr_claim_id = ?, status = ?, expected_decision_at = ?,"
                    " attempts = attempts + 1, submitted_at = ? WHERE id = ?",
                    (
                        result.hr_claim_id,
                        result.status,
                        utc_iso(result.expected_decision_at) if result.expected_decision_at else None,
                        utc_iso(now),
                        payload["claim_id"],
                    ),
                )
                conn.execute(
                    "UPDATE outbox SET processed_at = ?, attempts = attempts + 1 WHERE id = ?",
                    (utc_iso(now), event["id"]),
                )
                if claim is not None:
                    self._apply_claim_decision(conn, claim, result, now, notify_pending=True)
                self._audit(
                    conn, None, "claim", payload["claim_id"], "submitted_to_hr",
                    {"hr_claim_id": result.hr_claim_id},
                )
            sent += 1
        return sent

    def _apply_claim_decision(self, conn, claim: dict, result, now: datetime, notify_pending: bool = False) -> None:
        if claim["status"] not in ("PENDING", "SUBMITTING"):
            return  # idempotency: an already-decided claim is never applied twice
        buyer = self._employee(conn, claim["employee_id"])
        conn.execute(
            "UPDATE claims SET status = ?, decided_at = ?, reimbursed_amount_minor = ?,"
            " reimbursed_currency = ?, reject_reason = ? WHERE id = ?",
            (
                result.status if result.status != "PENDING" else "PENDING",
                utc_iso(now) if result.status != "PENDING" else None,
                result.reimbursed_amount_minor,
                result.reimbursed_currency,
                result.reject_reason,
                claim["id"],
            ),
        )
        refills = many(
            conn,
            "SELECT r.*, o.name AS office_name, o.timezone AS office_timezone FROM refills r"
            " JOIN purchase_lines l ON l.refill_id = r.id"
            " JOIN offices o ON o.id = r.office_id"
            " WHERE l.purchase_id = ?",
            (claim["purchase_id"],),
        )
        office = self._office(conn, refills[0]["office_id"]) if refills else None
        channel = self._channel(office) if office else "#snacks"

        if result.status == "APPROVED":
            for refill in refills:
                if refill["status"] in ("PURCHASED", "NEEDS_ATTENTION"):
                    self._transition(
                        conn,
                        refill["id"],
                        ("PURCHASED", "NEEDS_ATTENTION"),
                        refill["version"],
                        status="REIMBURSED",
                    )
            conn.execute(
                "INSERT INTO fairness (employee_id, completed_count, last_completed_at)"
                " VALUES (?, 1, ?) ON CONFLICT(employee_id) DO UPDATE SET"
                " completed_count = completed_count + 1, last_completed_at = excluded.last_completed_at",
                (buyer["id"], utc_iso(now)),
            )
            payout = (
                Money(result.reimbursed_amount_minor, result.reimbursed_currency)
                if result.reimbursed_amount_minor is not None
                else Money(claim["amount_minor"], claim["currency"])
            )
            self._notify(
                conn,
                channel,
                f"HR approved {Money(claim['amount_minor'], claim['currency'])} for {buyer['name']}. "
                f"Payout: {payout}.",
                kind="reimbursed",
                employee_id=buyer["id"],
            )
            self._audit(conn, None, "claim", claim["id"], "approved", {"payout_minor": payout.minor})
        elif result.status == "REJECTED":
            for refill in refills:
                if refill["status"] == "PURCHASED":
                    self._transition(
                        conn,
                        refill["id"],
                        ("PURCHASED",),
                        refill["version"],
                        status="NEEDS_ATTENTION",
                    )
            self._notify(
                conn,
                channel,
                f":warning: HR rejected {buyer['name']}'s claim "
                f"({Money(claim['amount_minor'], claim['currency'])}): {result.reject_reason}. "
                f"Spend stays in the ledger; a manager needs to fix the claim.",
                kind="alert",
                employee_id=buyer["id"],
            )
            self._audit(
                conn, None, "claim", claim["id"], "rejected", {"reason": result.reject_reason}
            )
        elif notify_pending:
            eta = (
                fmt_local(result.expected_decision_at, office["timezone"])
                if office and result.expected_decision_at
                else "1-10 days"
            )
            self._notify(
                conn,
                channel,
                f"Claim {result.hr_claim_id} submitted for {buyer['name']} "
                f"({Money(claim['amount_minor'], claim['currency'])}). HR decision expected around {eta}.",
                kind="claim_submitted",
                employee_id=buyer["id"],
            )

    # -- scheduling -------------------------------------------------------
    def _poll_claims(self) -> int:
        now = self.clock.now()
        rows = many(
            self.db.conn,
            "SELECT * FROM claims WHERE status = 'PENDING' AND hr_claim_id IS NOT NULL"
            " AND expected_decision_at IS NOT NULL AND expected_decision_at <= ?",
            (utc_iso(now),),
        )
        applied = 0
        for claim in rows:
            try:
                result = self.hr.get_claim(claim["hr_claim_id"])
            except Exception:
                continue
            with self.db.tx():
                conn = self.db.conn
                fresh = one(conn, "SELECT * FROM claims WHERE id = ?", (claim["id"],))
                if fresh is None or fresh["status"] != "PENDING":
                    continue
                if result.status != "PENDING":
                    self._apply_claim_decision(conn, fresh, result, now)
                    applied += 1
        return applied

    def _sweep_offers(self, conn, now: datetime) -> None:
        rows = many(
            conn,
            "SELECT * FROM refills WHERE status IN ('OFFERED','BROADCAST')"
            " AND offer_expires_at IS NOT NULL AND offer_expires_at <= ?",
            (utc_iso(now),),
        )
        for refill in rows:
            office = self._office(conn, refill["office_id"])
            conn.execute(
                "UPDATE offers SET status = 'expired', responded_at = ?"
                " WHERE refill_id = ? AND status = 'offered'",
                (utc_iso(now), refill["id"]),
            )
            if refill["status"] == "BROADCAST":
                self._transition(
                    conn,
                    refill["id"],
                    ("BROADCAST",),
                    refill["version"],
                    status="OPEN",
                    blocked_reason="UNASSIGNED",
                    offered_to_id=None,
                    offer_expires_at=None,
                )
                self._notify(
                    conn,
                    self._channel(office),
                    f":warning: Nobody accepted {office['name']}'s empty snack box before the deadline. "
                    f"A manager needs to step in.",
                    kind="alert",
                    refill_id=refill["id"],
                    employee_id=office["manager_employee_id"],
                )
                self._audit(conn, None, "refill", refill["id"], "unassigned", {})
            else:
                if refill["offered_to_id"]:
                    conn.execute(
                        "INSERT OR IGNORE INTO refill_exclusions"
                        " (refill_id, employee_id, reason, created_at) VALUES (?, ?, 'no_response', ?)",
                        (refill["id"], refill["offered_to_id"], utc_iso(now)),
                    )
                self._transition(
                    conn,
                    refill["id"],
                    ("OFFERED",),
                    refill["version"],
                    status="OPEN",
                    offered_to_id=None,
                    offer_expires_at=None,
                )
                self._audit(conn, None, "refill", refill["id"], "offer_expired", {})
                self._offer_next(conn, refill["id"])

    def _sweep_assigned(self, conn, now: datetime) -> None:
        rows = many(
            conn,
            "SELECT * FROM refills WHERE status = 'ASSIGNED' AND due_at IS NOT NULL AND due_at <= ?",
            (utc_iso(now),),
        )
        for refill in rows:
            office = self._office(conn, refill["office_id"])
            due = parse_iso(refill["due_at"])
            overdue_by = now - due
            if overdue_by >= timedelta(hours=self.settings.grace_hours):
                self._abandon(conn, refill, office, now)
            elif refill["reminders_sent"] < self.settings.max_reminders:
                conn.execute(
                    "UPDATE refills SET reminders_sent = reminders_sent + 1 WHERE id = ?",
                    (refill["id"],),
                )
                self._notify(
                    conn,
                    self._channel(office),
                    f"{self._employee(conn, refill['assignee_id'])['name']}, the deadline for the snack "
                    f"refill has passed. Still on it? Otherwise we'll hand it to someone else.",
                    kind="reminder",
                    refill_id=refill["id"],
                    employee_id=refill["assignee_id"],
                    actions=[{"id": "still_on_it", "label": "Still on it"}],
                )
                self._audit(conn, None, "refill", refill["id"], "reminder_sent", {})

    def _abandon(self, conn, refill: dict, office: dict, now: datetime) -> None:
        assignee = self._employee(conn, refill["assignee_id"])
        conn.execute(
            "INSERT INTO fairness (employee_id, abandoned_count) VALUES (?, 1)"
            " ON CONFLICT(employee_id) DO UPDATE SET abandoned_count = abandoned_count + 1",
            (assignee["id"],),
        )
        conn.execute(
            "INSERT OR IGNORE INTO refill_exclusions (refill_id, employee_id, reason, created_at)"
            " VALUES (?, ?, 'abandoned', ?)",
            (refill["id"], assignee["id"], utc_iso(now)),
        )
        self._transition(
            conn,
            refill["id"],
            ("ASSIGNED",),
            refill["version"],
            status="OPEN",
            assignee_id=None,
            offered_to_id=None,
            offer_expires_at=None,
            due_at=None,
            reminders_sent=0,
            abandoned_count=refill["abandoned_count"] + 1,
        )
        self._notify(
            conn,
            self._channel(office),
            f"{assignee['name']} didn't complete the refill in time. Handing it to someone else now.",
            kind="handover",
            refill_id=refill["id"],
        )
        self._audit(conn, None, "refill", refill["id"], "abandoned", {"assignee_id": assignee["id"]})
        self._offer_next(conn, refill["id"])

    def sweep(self) -> dict:
        """One idempotent tick: retry HR, poll decisions, expire offers, chase late buyers."""
        self.deliver_outbox()
        decisions = self._poll_claims()
        now = self.clock.now()
        with self.db.tx():
            conn = self.db.conn
            self._sweep_offers(conn, now)
            self._sweep_assigned(conn, now)
        return {
            "now": utc_iso(now),
            "claim_decisions": decisions,
            "open_refills": int(
                scalar(conn, "SELECT COUNT(*) FROM refills WHERE status IN ('OPEN','OFFERED','BROADCAST','ASSIGNED')")
                or 0
            ),
        }

    # -- managers ---------------------------------------------------------
    def override_budget(self, office_id: str, actor_id: str, reason: str, period: str | None = None) -> dict:
        now = self.clock.now()
        with self.db.tx():
            conn = self.db.conn
            office = self._office(conn, office_id)
            self._employee(conn, actor_id)
            period = period or self._period(office, now)
            conn.execute(
                "INSERT INTO budget_overrides (office_id, period, actor_id, reason, created_at)"
                " VALUES (?, ?, ?, ?, ?) ON CONFLICT(office_id, period) DO UPDATE SET"
                " actor_id = excluded.actor_id, reason = excluded.reason, created_at = excluded.created_at",
                (office_id, period, actor_id, reason, utc_iso(now)),
            )
            held = many(
                conn,
                "SELECT * FROM refills WHERE office_id = ? AND status = 'OPEN' AND blocked_reason = 'BUDGET'",
                (office_id,),
            )
            for refill in held:
                conn.execute(
                    "UPDATE refills SET blocked_reason = NULL, version = version + 1 WHERE id = ?",
                    (refill["id"],),
                )
                fresh = self._raw_refill(conn, refill["id"])
                if self._reserve(conn, fresh, office, now):
                    self._offer_next(conn, refill["id"])
            self._notify(
                conn,
                self._channel(office),
                f"Budget override approved by {self._employee(conn, actor_id)['name']} "
                f"for {period}: {reason}",
                kind="override",
            )
            self._audit(conn, actor_id, "office", office_id, "budget_override", {"period": period, "reason": reason})
        return self.budget_status(office_id, period)

    # -- dashboard helpers ------------------------------------------------
    def set_presence(self, employee_id: str, on_date: str, status: str, office_id: str | None = None) -> None:
        if status not in ("office", "remote", "leave"):
            raise ValidationError("status must be office, remote or leave")
        with self.db.tx():
            conn = self.db.conn
            employee = self._employee(conn, employee_id)
            conn.execute(
                "INSERT INTO presence (employee_id, on_date, office_id, status) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(employee_id, on_date) DO UPDATE SET office_id = excluded.office_id,"
                " status = excluded.status",
                (employee_id, on_date, office_id or employee["home_office_id"], status),
            )

    def bootstrap(self, period: str | None = None) -> dict:
        conn = self.db.conn
        now = self.clock.now()
        offices = many(conn, "SELECT * FROM offices ORDER BY name")
        for office in offices:
            office["budget"] = self.budget_status(office["id"], period)
        boxes = many(
            conn,
            """
            SELECT b.*, o.name AS office_name, o.timezone AS office_timezone,
                   r.id AS active_refill_id, r.status AS active_refill_status,
                   r.assignee_id, e.name AS assignee_name
              FROM boxes b
              JOIN offices o ON o.id = b.office_id
              LEFT JOIN refills r ON r.box_id = b.id
                   AND r.status IN ('OPEN','OFFERED','BROADCAST','ASSIGNED')
              LEFT JOIN employees e ON e.id = r.assignee_id
             ORDER BY o.name, b.label
            """,
        )
        employees = many(
            conn,
            "SELECT e.*, o.name AS office_name, COALESCE(f.completed_count,0) AS completed_count"
            " FROM employees e JOIN offices o ON o.id = e.home_office_id"
            " LEFT JOIN fairness f ON f.employee_id = e.id ORDER BY o.name, e.name",
        )
        return {
            "now": utc_iso(now),
            "offices": offices,
            "boxes": boxes,
            "employees": employees,
            "refills": self.list_refills(30),
            "messages": getattr(self.chat, "transcript", lambda limit=40: [])(40),
        }
