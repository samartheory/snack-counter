from __future__ import annotations

from datetime import datetime, timezone

from conftest import World


def test_reservation_then_spend_replaces_the_estimate(world: World) -> None:
    before = world.svc.budget_status("osl")
    refill = world.report("osl", "Almonds & nuts")
    reserved = world.svc.budget_status("osl")
    assert reserved["reserved_minor"] == refill["expected_cost_minor"] == 75_000
    assert reserved["available_minor"] == before["available_minor"] - 75_000
    assert reserved["spent_minor"] == 0

    world.accept_offered(refill["id"])
    world.purchase(refill["id"], 64_000)
    after = world.svc.budget_status("osl")
    assert after["reserved_minor"] == 0
    assert after["spent_minor"] == 64_000
    assert after["available_minor"] == before["budget_minor"] - 64_000


def test_budgets_are_independent_per_office_and_currency(world: World) -> None:
    oslo = world.report("osl", "Almonds & nuts")
    world.accept_offered(oslo["id"])
    world.purchase(oslo["id"], 64_000)

    blr = world.svc.budget_status("blr")
    assert blr["spent_minor"] == 0
    assert blr["reserved_minor"] == 0
    assert blr["budget_minor"] == 1_500_000


def test_over_budget_blocks_assignment_until_manager_override(world: World) -> None:
    world.db.conn.execute("UPDATE offices SET monthly_budget_minor = 10_000 WHERE id = 'osl'")

    refill = world.report("osl", "Chocolate")
    assert refill["status"] == "OPEN"
    assert refill["blocked_reason"] == "BUDGET"
    assert refill["offered_to_id"] is None
    assert refill["reserved_minor"] == 0
    assert "budget_blocked" in world.audit_actions()

    status = world.svc.override_budget("osl", world.office("osl")["manager_id"], "Month-end top-up")
    assert status["overridden"] is True

    after = world.svc.refill_view(refill["id"])
    assert after["status"] == "OFFERED"
    assert after["reserved_minor"] == 75_000
    assert after["blocked_reason"] is None

    budget = world.svc.budget_status("osl")
    assert budget["available_minor"] < 0
    assert budget["overridden"] is True
    assert "budget_override" in world.audit_actions()


def test_cancel_returns_the_reservation_to_the_pool(world: World) -> None:
    refill = world.report()
    world.svc.cancel(refill["id"], world.emp("osl", 1), "Duplicate report")
    status = world.svc.budget_status("osl")
    assert status["reserved_minor"] == 0
    entries = [
        row["kind"]
        for row in world.db.conn.execute(
            "SELECT kind FROM budget_entries WHERE refill_id = ? ORDER BY rowid", (refill["id"],)
        ).fetchall()
    ]
    assert entries == ["reserve", "release"]


def test_reserve_from_a_previous_month_is_released_into_that_month(world: World) -> None:
    world.clock.set(datetime(2026, 11, 30, 22, 0, tzinfo=timezone.utc))
    refill = world.report("osl", "Fruit & bars", actor=world.emp("osl", 1))

    world.clock.set(datetime(2026, 12, 1, 9, 0, tzinfo=timezone.utc))
    world.svc.cancel(refill["id"], world.emp("osl", 1), "Stale")

    november = world.svc.budget_status("osl", "2026-11")
    december = world.svc.budget_status("osl", "2026-12")
    assert november["reserved_minor"] == 0
    assert december["reserved_minor"] == 0
