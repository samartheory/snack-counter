from __future__ import annotations

from snackcounter.hr import FakeHr, UnavailableHr
from snackcounter.service import SnackService
from conftest import World


def _buy(world: World, key: str = "osl", label: str = "Almonds & nuts", amount: int = 64_000) -> tuple[str, str]:
    refill = world.report(key, label)
    world.accept_offered(refill["id"])
    world.purchase(refill["id"], amount)
    claim = world.db.conn.execute(
        "SELECT * FROM claims WHERE purchase_id ="
        " (SELECT purchase_id FROM refills WHERE id = ?)",
        (refill["id"],),
    ).fetchone()
    return refill["id"], claim["purchase_id"]


def test_claim_is_created_once_even_if_the_outbox_retries(world: World) -> None:
    refill_id, _ = _buy(world)
    assert world.db.conn.execute("SELECT COUNT(*) FROM hr_claims").fetchone()[0] == 1

    # Pretend the "message delivered" acknowledgement was lost and we retry.
    world.db.conn.execute("UPDATE outbox SET processed_at = NULL")
    world.svc.deliver_outbox()

    assert world.db.conn.execute("SELECT COUNT(*) FROM hr_claims").fetchone()[0] == 1
    claim = world.db.conn.execute(
        "SELECT status, hr_claim_id FROM claims WHERE purchase_id ="
        " (SELECT purchase_id FROM refills WHERE id = ?)",
        (refill_id,),
    ).fetchone()
    assert claim["status"] == "PENDING"
    assert claim["hr_claim_id"] is not None


def test_hr_outage_keeps_the_claim_queued_until_the_next_tick(world: World) -> None:
    broken = SnackService(world.db, clock=world.clock, hr=UnavailableHr())
    refill = broken.report_empty(world.box("osl"), world.emp("osl", 4))
    broken.accept(refill["id"], refill["offered_to_id"])
    broken.record_purchase(
        refill["offered_to_id"],
        [{"refill_id": refill["id"], "amount_minor": 64_000}],
        "NOK",
        "receipts/offline.jpg",
    )

    claim = world.db.conn.execute(
        "SELECT status, attempts FROM claims WHERE purchase_id ="
        " (SELECT purchase_id FROM refills WHERE id = ?)",
        (refill["id"],),
    ).fetchone()
    assert claim["status"] == "SUBMITTING"
    assert claim["attempts"] >= 1
    assert world.db.conn.execute("SELECT COUNT(*) FROM hr_claims").fetchone()[0] == 0

    broken.hr = FakeHr(world.db, world.clock)
    broken.sweep()

    recovered = world.db.conn.execute(
        "SELECT status, hr_claim_id FROM claims WHERE purchase_id ="
        " (SELECT purchase_id FROM refills WHERE id = ?)",
        (refill["id"],),
    ).fetchone()
    assert recovered["status"] == "PENDING"
    assert world.db.conn.execute("SELECT COUNT(*) FROM hr_claims").fetchone()[0] == 1


def test_rejected_claim_keeps_the_spend_and_flags_the_refill(world: World) -> None:
    refill_id, purchase_id = _buy(world)
    world.svc.hr.reject_keys.add(purchase_id)  # type: ignore[attr-defined]

    world.advance(days=11)
    world.sweep()

    claim = world.db.conn.execute(
        "SELECT status, reject_reason FROM claims WHERE purchase_id = ?", (purchase_id,)
    ).fetchone()
    assert claim["status"] == "REJECTED"
    assert claim["reject_reason"]

    refill = world.svc.refill_view(refill_id)
    assert refill["status"] == "NEEDS_ATTENTION"
    assert world.svc.budget_status("osl")["spent_minor"] == 64_000
    assert "rejected" in world.audit_actions()


def test_hr_reimburses_in_the_payroll_currency_of_the_employee(world: World) -> None:
    refill = world.report("blr", "Namkeen")
    # An Oslo employee on a visit buys in Bengaluru: INR receipt, NOK payroll.
    world.svc.record_purchase(
        world.emp("osl", 9),
        [{"refill_id": refill["id"], "amount_minor": 100_000}],  # 1 000 INR
        "INR",
        "receipts/visitor.jpg",
        allow_rescue=True,
    )

    world.advance(days=11)
    world.sweep()

    claim = world.db.conn.execute("SELECT * FROM claims").fetchone()
    assert claim["status"] == "APPROVED"
    assert claim["currency"] == "INR"
    assert claim["reimbursed_currency"] == "NOK"
    assert claim["reimbursed_amount_minor"] == 12_048  # HR's own rate, not ours
