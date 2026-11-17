from __future__ import annotations

import sqlite3
import threading

from snackcounter.db import Database
from snackcounter.errors import Conflict
from snackcounter.service import SnackService
from conftest import World


def _service_on_same_file(world: World, clock) -> SnackService:
    db = Database(world.path)
    return SnackService(db, clock=clock, settings=world.settings)


def test_only_one_of_two_simultaneous_accepts_wins(world: World) -> None:
    refill = world.make_broadcast()
    first = world.emp("osl", 10)
    second = world.emp("osl", 11)

    barrier = threading.Barrier(2)
    results: list[tuple[str, str]] = []
    lock = threading.Lock()

    def attempt(employee_id: str) -> None:
        service = _service_on_same_file(world, world.clock)
        try:
            barrier.wait(timeout=10)
            service.accept(refill["id"], employee_id)
            with lock:
                results.append(("accepted", employee_id))
        except Conflict:
            with lock:
                results.append(("rejected", employee_id))
        except Exception as exc:  # pragma: no cover - surfaced in assertion below
            with lock:
                results.append((f"error:{type(exc).__name__}:{exc}", employee_id))
        finally:
            service.db.close()

    threads = [threading.Thread(target=attempt, args=(emp,)) for emp in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    accepted = [emp for status, emp in results if status == "accepted"]
    rejected = [emp for status, emp in results if status == "rejected"]
    assert len(accepted) == 1, results
    assert len(rejected) == 1, results
    assert not [s for s, _ in results if s.startswith("error")], results

    final = world.svc.refill_view(refill["id"])
    assert final["status"] == "ASSIGNED"
    assert final["assignee_id"] == accepted[0]


def test_only_one_of_two_simultaneous_empty_reports_wins(world: World) -> None:
    box_id = world.box("osl", "Chocolate")
    barrier = threading.Barrier(2)
    results: list[str] = []
    lock = threading.Lock()

    def report(actor: str) -> None:
        service = _service_on_same_file(world, world.clock)
        try:
            barrier.wait(timeout=10)
            service.report_empty(box_id, actor)
            with lock:
                results.append("created")
        except Conflict:
            with lock:
                results.append("conflict")
        except sqlite3.IntegrityError:
            with lock:
                results.append("integrity")
        finally:
            service.db.close()

    threads = [
        threading.Thread(target=report, args=(world.emp("osl", 5),)),
        threading.Thread(target=report, args=(world.emp("osl", 6),)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert sorted(results) == ["conflict", "created"], results
    active = world.db.conn.execute(
        "SELECT COUNT(*) FROM refills WHERE box_id = ?"
        " AND status IN ('OPEN','OFFERED','BROADCAST','ASSIGNED')",
        (box_id,),
    ).fetchone()[0]
    assert active == 1


def test_stale_version_transition_is_rejected(world: World) -> None:
    """Even if an application skipped the status check, the CAS would still fail."""
    refill = world.report()
    stale = dict(refill)
    world.accept_offered(refill["id"])

    try:
        with world.db.tx():
            world.svc._transition(  # noqa: SLF001 - this is the invariant under test
                world.db.conn,
                refill["id"],
                ("OFFERED", "BROADCAST"),
                stale["version"],
                status="ASSIGNED",
                assignee_id=world.emp("osl", 12),
            )
        assert False, "stale update should have been rejected"
    except Conflict:
        pass
