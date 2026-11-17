from __future__ import annotations

from datetime import timedelta

from conftest import World


def test_remote_and_leave_are_skipped(world: World) -> None:
    target = world.emp("osl", 3)
    for employee_id in world.office("osl")["employee_ids"]:
        if employee_id != target:
            world.set_presence(employee_id, "remote")

    refill = world.report(actor=world.emp("osl", 1))
    assert refill["offered_to_id"] == target


def test_employee_with_too_many_open_claims_is_skipped(world: World) -> None:
    target = world.emp("osl", 5)
    for employee_id in world.office("osl")["employee_ids"]:
        if employee_id == target:
            continue
        world.give_open_claim(employee_id)
        world.give_open_claim_second(employee_id)

    refill = world.report(actor=world.emp("osl", 1))
    assert refill["offered_to_id"] == target


def test_fairness_prefers_the_person_with_fewest_recent_refills(world: World) -> None:
    target = world.emp("osl", 8)
    for employee_id in world.office("osl")["employee_ids"]:
        if employee_id == target:
            continue
        world.db.conn.execute(
            "INSERT OR REPLACE INTO fairness (employee_id, completed_count, last_completed_at)"
            " VALUES (?, 4, '2026-11-10T10:00:00+00:00')",
            (employee_id,),
        )

    refill = world.report(actor=world.emp("osl", 1))
    assert refill["offered_to_id"] == target


def test_fairness_also_demotes_repeat_abandoners(world: World) -> None:
    target = world.emp("osl", 2)
    for employee_id in world.office("osl")["employee_ids"]:
        if employee_id == target:
            continue
        world.db.conn.execute(
            "INSERT OR REPLACE INTO fairness (employee_id, completed_count, last_completed_at,"
            " abandoned_count) VALUES (?, 0, NULL, 5)",
            (employee_id,),
        )

    refill = world.report(actor=world.emp("osl", 1))
    assert refill["offered_to_id"] == target


def test_cooldown_is_relaxed_when_nobody_else_is_available(world: World) -> None:
    now = world.clock.now()
    recent = (now - timedelta(days=1)).isoformat()
    for employee_id in world.office("osl")["employee_ids"]:
        world.db.conn.execute(
            "INSERT OR REPLACE INTO fairness (employee_id, completed_count, last_completed_at)"
            " VALUES (?, 1, ?)",
            (employee_id, recent),
        )

    refill = world.report(actor=world.emp("osl", 1))
    assert refill["offered_to_id"] is not None
    assert refill["status"] == "OFFERED"


def test_a_visitor_can_take_a_refill_in_the_other_office(world: World) -> None:
    visitor = world.emp("osl", 12)
    for employee_id in world.office("blr")["employee_ids"]:
        world.set_presence(employee_id, "remote", office_key="blr")
    world.set_presence(visitor, "office", office_key="blr")

    refill = world.report("blr", actor=world.emp("blr", 1))
    assert refill["status"] == "OFFERED"
    assert refill["offered_to_id"] == visitor
    assert refill["currency"] == "INR"

    accepted = world.svc.accept(refill["id"], visitor)
    assert accepted["status"] == "ASSIGNED"
    # The claim keeps the receipt currency; payroll currency is HR's concern.
    purchased = world.purchase(
        refill["id"], 90_000, buyer=visitor, currency="INR"
    )
    assert purchased["receipt_currency"] == "INR"


def test_broadcast_accept_requires_presence(world: World) -> None:
    refill = world.make_broadcast()
    absent = world.emp("osl", 14)
    world.set_presence(absent, "remote")
    try:
        world.svc.accept(refill["id"], absent)
        assert False, "an absent employee should not be able to accept a broadcast"
    except Exception as exc:
        assert "eligible" in str(exc).lower()
