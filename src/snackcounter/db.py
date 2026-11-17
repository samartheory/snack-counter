"""SQLite schema and connection handling.

Deliberately no ORM: the double-commit invariant is enforced by a conditional
UPDATE plus a partial unique index, and both should be visible in the code.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS offices (
    id                    TEXT PRIMARY KEY,
    name                  TEXT NOT NULL,
    currency              TEXT NOT NULL,
    monthly_budget_minor  INTEGER NOT NULL,
    typical_min_minor     INTEGER NOT NULL,
    typical_max_minor     INTEGER NOT NULL,
    timezone              TEXT NOT NULL,
    manager_employee_id   TEXT,
    holidays              TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS employees (
    id                TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    home_office_id    TEXT NOT NULL REFERENCES offices(id),
    hr_employee_id    TEXT NOT NULL UNIQUE,
    payroll_currency  TEXT NOT NULL,
    is_volunteer      INTEGER NOT NULL DEFAULT 1
);

-- One row per employee per day: where they physically are, or that they are away.
CREATE TABLE IF NOT EXISTS presence (
    employee_id  TEXT NOT NULL REFERENCES employees(id),
    on_date      TEXT NOT NULL,
    office_id    TEXT NOT NULL REFERENCES offices(id),
    status       TEXT NOT NULL CHECK (status IN ('office', 'remote', 'leave')),
    PRIMARY KEY (employee_id, on_date)
);

CREATE TABLE IF NOT EXISTS boxes (
    id         TEXT PRIMARY KEY,
    office_id  TEXT NOT NULL REFERENCES offices(id),
    label      TEXT NOT NULL,
    category   TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'stocked'
               CHECK (status IN ('stocked', 'low', 'empty'))
);

CREATE TABLE IF NOT EXISTS refills (
    id                TEXT PRIMARY KEY,
    box_id            TEXT NOT NULL REFERENCES boxes(id),
    office_id         TEXT NOT NULL REFERENCES offices(id),
    status            TEXT NOT NULL,
    version           INTEGER NOT NULL DEFAULT 0,
    assignee_id       TEXT REFERENCES employees(id),
    offered_to_id     TEXT REFERENCES employees(id),
    priority          TEXT NOT NULL DEFAULT 'normal',
    created_at        TEXT NOT NULL,
    offered_at        TEXT,
    offer_expires_at  TEXT,
    accepted_at       TEXT,
    due_at            TEXT,
    purchased_at      TEXT,
    expected_cost_minor INTEGER NOT NULL,
    currency          TEXT NOT NULL,
    reserved_minor    INTEGER NOT NULL DEFAULT 0,
    reserve_entry_id  TEXT,
    purchase_id       TEXT,
    blocked_reason    TEXT,
    reminders_sent    INTEGER NOT NULL DEFAULT 0,
    extension_count   INTEGER NOT NULL DEFAULT 0,
    abandoned_count   INTEGER NOT NULL DEFAULT 0,
    cancel_reason     TEXT
);

-- The invariant, enforced by the database rather than by hope:
-- at most one box can be in an active coordination state at a time.
CREATE UNIQUE INDEX IF NOT EXISTS one_active_refill_per_box
    ON refills(box_id)
    WHERE status IN ('OPEN', 'OFFERED', 'BROADCAST', 'ASSIGNED');

CREATE INDEX IF NOT EXISTS refills_status_idx ON refills(status);
CREATE INDEX IF NOT EXISTS refills_office_idx ON refills(office_id, created_at);

CREATE TABLE IF NOT EXISTS offers (
    id            TEXT PRIMARY KEY,
    refill_id     TEXT NOT NULL REFERENCES refills(id),
    employee_id   TEXT NOT NULL REFERENCES employees(id),
    rank          INTEGER NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('offered', 'accepted', 'declined', 'expired', 'superseded')),
    created_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    responded_at  TEXT,
    UNIQUE (refill_id, employee_id)
);

CREATE TABLE IF NOT EXISTS refill_exclusions (
    refill_id    TEXT NOT NULL REFERENCES refills(id),
    employee_id  TEXT NOT NULL REFERENCES employees(id),
    reason       TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (refill_id, employee_id)
);

CREATE TABLE IF NOT EXISTS purchases (
    id                   TEXT PRIMARY KEY,
    buyer_id             TEXT NOT NULL REFERENCES employees(id),
    purchased_at         TEXT NOT NULL,
    receipt_amount_minor INTEGER NOT NULL,
    receipt_currency     TEXT NOT NULL,
    receipt_ref          TEXT,
    created_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS purchase_lines (
    purchase_id         TEXT NOT NULL REFERENCES purchases(id),
    refill_id           TEXT NOT NULL REFERENCES refills(id),
    office_id           TEXT NOT NULL REFERENCES offices(id),
    currency            TEXT NOT NULL,
    amount_minor        INTEGER NOT NULL,
    office_currency     TEXT NOT NULL,
    amount_office_minor INTEGER NOT NULL,
    fx_rate             TEXT NOT NULL,
    purchased_on        TEXT NOT NULL,
    period              TEXT NOT NULL,
    PRIMARY KEY (purchase_id, refill_id)
);

CREATE INDEX IF NOT EXISTS purchase_lines_period_idx ON purchase_lines(period, office_id);

CREATE TABLE IF NOT EXISTS claims (
    id                     TEXT PRIMARY KEY,
    purchase_id            TEXT NOT NULL UNIQUE REFERENCES purchases(id),
    employee_id            TEXT NOT NULL REFERENCES employees(id),
    hr_claim_id            TEXT UNIQUE,
    amount_minor           INTEGER NOT NULL,
    currency               TEXT NOT NULL,
    status                 TEXT NOT NULL CHECK (status IN ('SUBMITTING', 'PENDING', 'APPROVED', 'REJECTED')),
    submitted_at           TEXT NOT NULL,
    expected_decision_at   TEXT,
    decided_at             TEXT,
    reimbursed_amount_minor INTEGER,
    reimbursed_currency    TEXT,
    reject_reason          TEXT,
    attempts               INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS budget_entries (
    id                  TEXT PRIMARY KEY,
    office_id           TEXT NOT NULL REFERENCES offices(id),
    period              TEXT NOT NULL,
    kind                TEXT NOT NULL CHECK (kind IN ('reserve', 'release', 'spend')),
    amount_minor        INTEGER NOT NULL,
    currency            TEXT NOT NULL,
    refill_id           TEXT,
    purchase_id         TEXT,
    source_amount_minor INTEGER,
    source_currency     TEXT,
    fx_rate             TEXT,
    created_at          TEXT NOT NULL,
    created_by          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS budget_entries_period_idx ON budget_entries(office_id, period);

CREATE TABLE IF NOT EXISTS budget_overrides (
    office_id   TEXT NOT NULL REFERENCES offices(id),
    period      TEXT NOT NULL,
    actor_id    TEXT NOT NULL,
    reason      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (office_id, period)
);

CREATE TABLE IF NOT EXISTS fairness (
    employee_id       TEXT PRIMARY KEY REFERENCES employees(id),
    completed_count   INTEGER NOT NULL DEFAULT 0,
    last_completed_at TEXT,
    abandoned_count   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS audit (
    id         TEXT PRIMARY KEY,
    at         TEXT NOT NULL,
    actor_id   TEXT,
    entity     TEXT NOT NULL,
    entity_id  TEXT NOT NULL,
    action     TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS messages (
    id          TEXT PRIMARY KEY,
    at          TEXT NOT NULL,
    channel     TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'info',
    employee_id TEXT,
    refill_id   TEXT,
    text        TEXT NOT NULL,
    actions     TEXT
);

CREATE TABLE IF NOT EXISTS outbox (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    payload      TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    processed_at TEXT,
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT
);

CREATE TABLE IF NOT EXISTS fx_rates (
    pair    TEXT NOT NULL,
    as_of   TEXT NOT NULL,
    rate    TEXT NOT NULL,
    source  TEXT NOT NULL,
    PRIMARY KEY (pair, as_of)
);

-- Simulated external system. Kept in its own table set so the fake can lie to us.
CREATE TABLE IF NOT EXISTS hr_claims (
    hr_claim_id             TEXT PRIMARY KEY,
    idempotency_key         TEXT NOT NULL UNIQUE,
    employee_id             TEXT NOT NULL,
    amount_minor            INTEGER NOT NULL,
    currency                TEXT NOT NULL,
    description             TEXT NOT NULL,
    receipt_ref             TEXT,
    submitted_at            TEXT NOT NULL,
    decision_at             TEXT NOT NULL,
    status                  TEXT NOT NULL CHECK (status IN ('PENDING', 'APPROVED', 'REJECTED')),
    payroll_currency        TEXT NOT NULL,
    reimbursed_amount_minor INTEGER,
    reimbursed_currency     TEXT,
    reject_reason           TEXT
);
"""


class Database:
    """A thin transaction wrapper. `BEGIN IMMEDIATE` makes writers queue instead of racing."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.conn = sqlite3.connect(
            self.path, isolation_level=None, check_same_thread=False, timeout=15
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 15000")
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self._lock = threading.RLock()

    def init_schema(self) -> None:
        self.conn.executescript(SCHEMA)

    @contextmanager
    def tx(self, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    def close(self) -> None:
        self.conn.close()


def init_db(path: str | Path = ":memory:") -> Database:
    db = Database(path)
    db.init_schema()
    return db


def one(conn: sqlite3.Connection, sql: str, params: tuple | list = ()) -> dict | None:
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row is not None else None


def many(conn: sqlite3.Connection, sql: str, params: tuple | list = ()) -> list[dict]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def scalar(conn: sqlite3.Connection, sql: str, params: tuple | list = ()):
    row = conn.execute(sql, params).fetchone()
    return row[0] if row is not None else None
