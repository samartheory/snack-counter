from __future__ import annotations

from datetime import datetime, timezone

import pytest

from snackcounter.clock import FakeClock
from snackcounter.config import Settings
from snackcounter.db import init_db, many
from snackcounter.seed import seed
from snackcounter.service import SnackService

START = datetime(2026, 11, 16, 8, 0, tzinfo=timezone.utc)  # Monday 09:00 in Oslo


class World:
    def __init__(self, tmp_path, settings: Settings | None = None) -> None:
        self.path = tmp_path / "snackcounter.sqlite"
        self.clock = FakeClock(START)
        self.settings = settings or Settings()
        self.db = init_db(self.path)
        with self.db.tx():
            self.directory = seed(self.db, self.clock)
        self.svc = SnackService(self.db, clock=self.clock, settings=self.settings)
        self._make_everyone_available()

    # -- setup helpers ----------------------------------------------------
    def _make_everyone_available(self) -> None:
        for key in ("osl", "blr"):
            office = self.directory[key]
            for employee_id in office["employee_ids"]:
                self.db.conn.execute(
                    "UPDATE presence SET status = 'office', office_id = ?"
                    " WHERE employee_id = ? AND on_date = '2026-11-16'",
                    (office["id"], employee_id),
                )

    def office(self, key: str = "osl") -> dict:
        return self.directory[key]

    def emp(self, key: str = "osl", index: int = 1) -> str:
        return self.directory[key]["employee_ids"][index - 1]

    def box(self, key: str = "osl", label: str | None = None) -> str:
        boxes = self.directory[key]["boxes"]
        return boxes[label or list(boxes)[0]]

    # -- domain helpers ---------------------------------------------------
    def report(self, key: str = "osl", label: str | None = None, actor: str | None = None) -> dict:
        return self.svc.report_empty(
            self.box(key, label), actor or self.emp(key, 4)
        )

    def accept_offered(self, refill_id: str) -> dict:
        view = self.svc.refill_view(refill_id)
        assert view["status"] == "OFFERED", view["status"]
        return self.svc.accept(refill_id, view["offered_to_id"])

    def purchase(
        self,
        refill_id: str,
        amount_minor: int,
        *,
        buyer: str | None = None,
        currency: str | None = None,
        allow_rescue: bool = False,
    ) -> dict:
        view = self.svc.refill_view(refill_id)
        return self.svc.record_purchase(
            buyer or view["assignee_id"] or self.emp(),
            [{"refill_id": refill_id, "amount_minor": amount_minor}],
            currency or view["currency"],
            "receipts/test.jpg",
            allow_rescue=allow_rescue,
        )

    def make_broadcast(self) -> dict:
        refill = self.report()
        first = self.svc.refill_view(refill["id"])
        self.svc.decline(refill["id"], first["offered_to_id"])
        second = self.svc.refill_view(refill["id"])
        self.svc.decline(refill["id"], second["offered_to_id"])
        view = self.svc.refill_view(refill["id"])
        assert view["status"] == "BROADCAST", view["status"]
        return view

    def give_open_claim(self, employee_id: str, seq: int = 1) -> None:
        """Direct insert: two unreimbursed claims block a volunteer from being offered more work."""
        suffix = f"{employee_id.replace('-', '')}{seq}"
        self.db.conn.execute(
            "INSERT INTO purchases (id, buyer_id, purchased_at, receipt_amount_minor,"
            " receipt_currency, receipt_ref, created_at) VALUES (?, ?, '2026-11-01T10:00:00+00:00',"
            " 1000, 'NOK', 'x.jpg', '2026-11-01T10:00:00+00:00')",
            (f"pu-{suffix}", employee_id),
        )
        self.db.conn.execute(
            "INSERT INTO claims (id, purchase_id, employee_id, amount_minor, currency, status,"
            " submitted_at) VALUES (?, ?, ?, 1000, 'NOK', 'PENDING', '2026-11-01T10:00:00+00:00')",
            (f"cl-{suffix}", f"pu-{suffix}", employee_id),
        )

    def give_open_claim_second(self, employee_id: str) -> None:
        self.give_open_claim(employee_id, seq=2)

    def set_presence(self, employee_id: str, status: str, office_key: str = "osl", on_date: str = "2026-11-16") -> None:
        self.db.conn.execute(
            "UPDATE presence SET status = ?, office_id = ? WHERE employee_id = ? AND on_date = ?",
            (status, self.office(office_key)["id"], employee_id, on_date),
        )

    # -- time -------------------------------------------------------------
    def advance(self, **kwargs) -> None:
        self.clock.advance(**kwargs)

    def sweep(self) -> dict:
        return self.svc.sweep()

    def claim_count(self) -> int:
        return self.db.conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0]

    def audit_actions(self) -> list[str]:
        return [row["action"] for row in many(self.db.conn, "SELECT action FROM audit ORDER BY at")]


@pytest.fixture
def world(tmp_path) -> World:
    w = World(tmp_path)
    yield w
    w.db.close()
