from __future__ import annotations

import sqlite3

import pytest

from snackcounter.errors import Conflict, ValidationError
from conftest import World


def test_happy_path_empty_to_reimbursed(world: World) -> None:
    refill = world.report("osl", "Almonds & nuts")
    assert refill["status"] == "OFFERED"
    assert refill["offered_to_id"] is not None
    assert refill["reserved_minor"] > 0

    assigned = world.accept_offered(refill["id"])
    assert assigned["status"] == "ASSIGNED"
    assert assigned["assignee_id"] == refill["offered_to_id"]
    assert assigned["due_at"] is not None

    purchased = world.purchase(refill["id"], 64_000)
    assert purchased["status"] == "PURCHASED"
    assert purchased["claim_status"] in ("SUBMITTING", "PENDING")

    world.advance(days=12)
    world.sweep()

    done = world.svc.refill_view(refill["id"])
    assert done["status"] == "REIMBURSED"
    assert done["claim_status"] == "APPROVED"
    assert done["reserved_minor"] == 0

    fairness = world.db.conn.execute(
        "SELECT completed_count FROM fairness WHERE employee_id = ?", (done["assignee_id"],)
    ).fetchone()
    assert fairness["completed_count"] == 1


def test_two_people_cannot_both_commit(world: World) -> None:
    refill = world.report()
    world.accept_offered(refill["id"])
    with pytest.raises(Conflict):
        world.svc.accept(refill["id"], world.emp("osl", 7))


def test_reporting_the_same_box_twice_is_rejected(world: World) -> None:
    first = world.report()
    with pytest.raises(Conflict) as exc:
        world.report()
    assert exc.value.context["refill_id"] == first["id"]


def test_a_decline_passes_the_refill_to_the_next_person(world: World) -> None:
    refill = world.report()
    first = refill["offered_to_id"]
    world.svc.decline(refill["id"], first, "Not in today")

    after = world.svc.refill_view(refill["id"])
    assert after["status"] == "OFFERED"
    assert after["offered_to_id"] != first
    assert after["offered_to_id"] is not None
    excluded = world.db.conn.execute(
        "SELECT reason FROM refill_exclusions WHERE refill_id = ? AND employee_id = ?",
        (refill["id"], first),
    ).fetchone()
    assert excluded["reason"] == "declined"


def test_expired_offer_is_reassigned_and_the_sweep_is_idempotent(world: World) -> None:
    refill = world.report()
    first = refill["offered_to_id"]

    world.advance(minutes=46)
    world.sweep()
    after = world.svc.refill_view(refill["id"])
    assert after["offered_to_id"] not in (None, first)

    stable = after["offered_to_id"]
    world.sweep()
    assert world.svc.refill_view(refill["id"])["offered_to_id"] == stable


def test_uncompleted_refill_is_reminded_then_handed_over(world: World) -> None:
    refill = world.report()
    accepted = world.accept_offered(refill["id"])
    original = accepted["assignee_id"]

    # Due is end of the next business day (Tue 18:00 Oslo = 17:00Z). 30 minutes late:
    world.advance(hours=33, minutes=30)
    world.sweep()
    reminded = world.svc.refill_view(refill["id"])
    assert reminded["status"] == "ASSIGNED"
    assert reminded["reminders_sent"] == 1

    # Past the 4h grace window: the refill moves on.
    world.advance(hours=5)
    world.sweep()
    after = world.svc.refill_view(refill["id"])
    assert after["status"] == "OFFERED"
    assert after["assignee_id"] is None
    assert after["offered_to_id"] != original
    assert after["abandoned_count"] == 1

    fairness = world.db.conn.execute(
        "SELECT abandoned_count FROM fairness WHERE employee_id = ?", (original,)
    ).fetchone()
    assert fairness["abandoned_count"] == 1

    excluded = world.db.conn.execute(
        "SELECT reason FROM refill_exclusions WHERE refill_id = ? AND employee_id = ?",
        (refill["id"], original),
    ).fetchone()
    assert excluded["reason"] == "abandoned"


def test_still_on_it_extends_the_deadline(world: World) -> None:
    refill = world.report()
    accepted = world.accept_offered(refill["id"])
    first_due = accepted["due_at"]
    world.advance(hours=2)
    extended = world.svc.mark_still_on_it(refill["id"], accepted["assignee_id"])
    assert extended["due_at"] > first_due
    assert extended["extension_count"] == 1


def test_cancel_releases_the_reservation_and_frees_the_box(world: World) -> None:
    refill = world.report()
    assert world.svc.budget_status("osl")["reserved_minor"] > 0

    cancelled = world.svc.cancel(refill["id"], world.emp("osl", 1), "Already refilled by a visitor")
    assert cancelled["status"] == "CANCELLED"
    assert world.svc.budget_status("osl")["reserved_minor"] == 0

    again = world.report()
    assert again["id"] != refill["id"]
    assert again["status"] == "OFFERED"


def test_purchase_requires_a_receipt_and_the_right_person(world: World) -> None:
    refill = world.report()
    accepted = world.accept_offered(refill["id"])
    owner = accepted["assignee_id"]

    with pytest.raises(ValidationError):
        world.svc.record_purchase(
            owner,
            [{"refill_id": refill["id"], "amount_minor": 50_000}],
            "NOK",
            "",
        )

    other = next(
        employee_id
        for employee_id in world.office("osl")["employee_ids"]
        if employee_id != owner
    )
    with pytest.raises(Conflict):
        world.svc.record_purchase(
            other,
            [{"refill_id": refill["id"], "amount_minor": 50_000}],
            "NOK",
            "receipts/x.jpg",
        )


def test_a_refill_cannot_be_purchased_twice(world: World) -> None:
    refill = world.report()
    accepted = world.accept_offered(refill["id"])
    world.purchase(refill["id"], 50_000)
    with pytest.raises(Conflict):
        world.purchase(refill["id"], 50_000, buyer=accepted["assignee_id"])


def test_a_box_bought_recently_cannot_be_reported_empty_again(world: World) -> None:
    refill = world.report()
    world.accept_offered(refill["id"])
    world.purchase(refill["id"], 50_000)

    with pytest.raises(Conflict) as exc:
        world.report(actor=world.emp("osl", 1))
    assert exc.value.context["status"] == "PURCHASED"

    world.advance(hours=7)
    again = world.report(actor=world.emp("osl", 1))
    assert again["status"] == "OFFERED"


def test_database_refuses_a_second_active_refill_even_bypassing_the_service(world: World) -> None:
    refill = world.report()
    with pytest.raises(sqlite3.IntegrityError):
        world.db.conn.execute(
            "INSERT INTO refills (id, box_id, office_id, status, version, created_at,"
            " expected_cost_minor, currency) VALUES ('rf-bypass', ?, 'osl', 'OPEN', 0,"
            " '2026-11-16T08:00:00+00:00', 75000, 'NOK')",
            (refill["box_id"],),
        )


def test_no_candidates_escalates_to_manager(world: World) -> None:
    for employee_id in world.office("osl")["employee_ids"]:
        world.set_presence(employee_id, "leave")

    refill = world.report()
    assert refill["status"] == "BROADCAST"

    world.advance(minutes=46)
    world.sweep()
    after = world.svc.refill_view(refill["id"])
    assert after["status"] == "OPEN"
    assert after["blocked_reason"] == "UNASSIGNED"
    assert "unassigned" in world.audit_actions()

    reopened = world.svc.reopen(refill["id"], world.emp("osl", 1))
    assert reopened["status"] == "BROADCAST"
