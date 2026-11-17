from __future__ import annotations

import json
import uuid
from typing import Protocol

from .clock import Clock, utc_iso


class ChatPort(Protocol):
    def send(
        self,
        channel: str,
        text: str,
        *,
        kind: str = "info",
        employee_id: str | None = None,
        refill_id: str | None = None,
        actions: list[dict] | None = None,
    ) -> None: ...


class FakeChat:
    """Stand-in for the company chat tool.

    Writes to the `messages` table so the dashboard and CLI can render the same
    transcript, and so tests can assert what people were told.
    """

    def __init__(self, db, clock: Clock) -> None:
        self.db = db
        self.clock = clock

    def send(
        self,
        channel: str,
        text: str,
        *,
        kind: str = "info",
        employee_id: str | None = None,
        refill_id: str | None = None,
        actions: list[dict] | None = None,
    ) -> None:
        self.db.conn.execute(
            "INSERT INTO messages (id, at, channel, kind, employee_id, refill_id, text, actions)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                uuid.uuid4().hex[:12],
                utc_iso(self.clock.now()),
                channel,
                kind,
                employee_id,
                refill_id,
                text,
                json.dumps(actions) if actions else None,
            ),
        )

    def transcript(self, limit: int = 200) -> list[dict]:
        rows = self.db.conn.execute(
            "SELECT * FROM messages ORDER BY at DESC, rowid DESC LIMIT ?", (limit,)
        ).fetchall()
        out = []
        for row in reversed(rows):
            item = dict(row)
            item["actions"] = json.loads(item["actions"]) if item["actions"] else []
            out.append(item)
        return out
