from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

from .config import Settings
from .db import many


def _tiebreak(refill_id: str, employee_id: str) -> str:
    # Stable across runs and machines, unlike Python's built-in hash().
    return hashlib.sha1(f"{refill_id}:{employee_id}".encode()).hexdigest()


def rank_candidates(
    conn,
    *,
    office_id: str,
    refill_id: str,
    on_date: str,
    now: datetime,
    exclude_ids: set[str],
    settings: Settings,
    allow_cooling: bool = False,
) -> list[dict]:
    """Who should be asked to buy, in order.

    Hard filters first (presence, capacity, cash-flow guardrail), then fairness.
    Ranking is deterministic so the same state always produces the same order.
    """
    cutoff_90 = (now - timedelta(days=90)).isoformat()
    rows = many(
        conn,
        """
        SELECT e.id, e.name, e.is_volunteer, e.home_office_id,
               p.status AS presence_status, p.office_id AS presence_office,
               COALESCE(f.completed_count, 0) AS completed_count,
               COALESCE(f.abandoned_count, 0) AS abandoned_count,
               f.last_completed_at,
               (SELECT COUNT(*) FROM claims c
                 WHERE c.employee_id = e.id AND c.status IN ('SUBMITTING', 'PENDING')) AS open_claims,
               (SELECT COUNT(*) FROM refills r2
                 WHERE r2.assignee_id = e.id
                   AND r2.status IN ('OFFERED', 'ASSIGNED', 'PURCHASED')) AS active_refills,
               (SELECT COUNT(*) FROM refills r3
                 WHERE r3.assignee_id = e.id AND r3.status = 'REIMBURSED'
                   AND r3.purchased_at >= ?) AS completed_90d
          FROM employees e
          JOIN presence p ON p.employee_id = e.id AND p.on_date = ?
          LEFT JOIN fairness f ON f.employee_id = e.id
         WHERE p.office_id = ? AND p.status = 'office'
        """,
        (cutoff_90, on_date, office_id),
    )

    eligible = [
        r
        for r in rows
        if r["id"] not in exclude_ids
        and r["open_claims"] < settings.max_open_claims
        and r["active_refills"] == 0
    ]

    # Volunteers are the default owners; everyone on site is a fallback so a box
    # never stays empty because the roster happens to be away.
    volunteers = [r for r in eligible if r["is_volunteer"]]
    pool = volunteers or eligible
    if not pool:
        return []

    if not allow_cooling:
        cooldown_cutoff = (now - timedelta(days=settings.cooldown_days)).isoformat()
        fresh = [
            r for r in pool if not r["last_completed_at"] or r["last_completed_at"] < cooldown_cutoff
        ]
        if fresh:
            pool = fresh

    pool.sort(
        key=lambda r: (
            r["completed_90d"],
            r["completed_count"],
            r["last_completed_at"] or "",
            r["abandoned_count"],
            _tiebreak(refill_id, r["id"]),
        )
    )
    return pool


def is_eligible_now(
    conn,
    *,
    refill: dict,
    employee_id: str,
    on_date: str,
    settings: Settings,
) -> bool:
    """Lighter check used when someone accepts a broadcast."""
    row = conn.execute(
        """
        SELECT e.id,
               p.status AS presence_status, p.office_id AS presence_office,
               (SELECT COUNT(*) FROM claims c
                 WHERE c.employee_id = e.id AND c.status IN ('SUBMITTING', 'PENDING')) AS open_claims,
               (SELECT COUNT(*) FROM refills r
                 WHERE r.assignee_id = e.id
                   AND r.status IN ('OFFERED', 'ASSIGNED', 'PURCHASED')
                   AND r.id != ?) AS active_refills
          FROM employees e
          JOIN presence p ON p.employee_id = e.id AND p.on_date = ?
         WHERE e.id = ?
        """,
        (refill["id"], on_date, employee_id),
    ).fetchone()
    if row is None:
        return False
    return (
        row["presence_status"] == "office"
        and row["presence_office"] == refill["office_id"]
        and row["open_claims"] < settings.max_open_claims
        and row["active_refills"] == 0
    )
