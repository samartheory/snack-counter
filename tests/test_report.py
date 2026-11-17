from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from snackcounter.money import Currency, Money, convert
from conftest import World


def _build_november(world: World) -> None:
    oslo = world.report("osl", "Almonds & nuts")
    world.accept_offered(oslo["id"])
    world.purchase(oslo["id"], 64_000)  # 640 NOK

    blr = world.report("blr", "Namkeen")
    world.accept_offered(blr["id"])
    world.purchase(blr["id"], 145_000)  # 1 450 INR

    # A visitor orders for the Bengaluru box but pays in NOK: the expense is charged
    # to the Bengaluru budget, converted at the purchase-date rate.
    cross = world.report("blr", "Biscuits")
    world.svc.record_purchase(
        world.emp("osl", 10),
        [{"refill_id": cross["id"], "amount_minor": 10_000}],  # 100 NOK
        "NOK",
        "receipts/cross-currency.jpg",
        allow_rescue=True,
    )


def test_monthly_totals_per_currency_and_grand_total(world: World) -> None:
    _build_november(world)
    report = __import__("snackcounter.report", fromlist=["monthly_report"]).monthly_report(
        world.db, period="2026-11"
    )

    offices = {row["office_name"]: row for row in report["offices"]}
    assert offices["Oslo"]["spent_minor"] == 64_000
    assert offices["Oslo"]["currency"] == "NOK"
    assert offices["Bengaluru"]["spent_minor"] == 229_200  # 1 450 INR + 842 INR
    assert offices["Bengaluru"]["currency"] == "INR"

    subtotals = {row["currency"]: row["minor"] for row in report["totals_by_currency"]}
    assert subtotals == {"NOK": 64_000, "INR": 229_200}

    expected_grand = 64_000 + convert(Money(229_200, Currency.INR), Currency.NOK, "0.11876").minor
    assert report["grand_total"]["minor"] == expected_grand == 91_220
    assert report["grand_total"]["currency"] == "NOK"
    assert report["grand_total"]["rates"][0]["rate"] == "0.11876"

    outstanding = {row["currency"]: row for row in report["outstanding_by_currency"]}
    assert outstanding["NOK"]["minor"] == 74_000  # 640 + 100, still with HR
    assert outstanding["INR"]["minor"] == 145_000


def test_approval_does_not_change_spend_but_clears_outstanding(world: World) -> None:
    _build_november(world)
    world.advance(days=12)
    world.sweep()

    report = __import__("snackcounter.report", fromlist=["monthly_report"]).monthly_report(
        world.db, period="2026-11"
    )
    assert {row["currency"]: row["minor"] for row in report["totals_by_currency"]} == {
        "NOK": 64_000,
        "INR": 229_200,
    }
    assert report["outstanding_by_currency"] == []


def test_month_attribution_uses_the_office_local_purchase_date(world: World) -> None:
    world.clock.set(datetime(2026, 11, 30, 22, 0, tzinfo=timezone.utc))  # 23:00 Oslo
    late_nov = world.report("osl", "Fruit & bars", actor=world.emp("osl", 1))
    world.svc.record_purchase(
        world.emp("osl", 2),
        [{"refill_id": late_nov["id"], "amount_minor": 10_000}],
        "NOK",
        "receipts/late-nov.jpg",
        allow_rescue=True,
    )

    world.clock.set(datetime(2026, 11, 30, 23, 30, tzinfo=timezone.utc))  # 00:30 Oslo, 1 Dec
    early_dec = world.report("osl", "Chocolate", actor=world.emp("osl", 1))
    world.svc.record_purchase(
        world.emp("osl", 2),
        [{"refill_id": early_dec["id"], "amount_minor": 20_000}],
        "NOK",
        "receipts/early-dec.jpg",
        allow_rescue=True,
    )

    from snackcounter.report import monthly_report

    november = monthly_report(world.db, period="2026-11")
    december = monthly_report(world.db, period="2026-12")
    oslo_nov = next(o for o in november["offices"] if o["office_id"] == "osl")
    oslo_dec = next(o for o in december["offices"] if o["office_id"] == "osl")
    assert oslo_nov["spent_minor"] == 10_000
    assert oslo_dec["spent_minor"] == 20_000


def test_claim_approved_in_a_later_month_stays_in_the_purchase_month(world: World) -> None:
    world.clock.set(datetime(2027, 1, 31, 22, 0, tzinfo=timezone.utc))  # 23:00 Oslo, 31 Jan
    refill = world.report("osl", "Almonds & nuts", actor=world.emp("osl", 1))
    world.svc.record_purchase(
        world.emp("osl", 2),
        [{"refill_id": refill["id"], "amount_minor": 30_000}],
        "NOK",
        "receipts/jan.jpg",
        allow_rescue=True,
    )

    world.clock.set(datetime(2027, 2, 12, 10, 0, tzinfo=timezone.utc))
    world.sweep()

    from snackcounter.report import monthly_report

    january = monthly_report(world.db, period="2027-01")
    february = monthly_report(world.db, period="2027-02")
    assert next(o for o in january["offices"] if o["office_id"] == "osl")["spent_minor"] == 30_000
    assert next(o for o in february["offices"] if o["office_id"] == "osl")["spent_minor"] == 0
    assert january["outstanding_by_currency"] == []


def test_report_uses_a_rate_snapshot_not_todays_rate(world: World) -> None:
    refill = world.report("blr", "Biscuits")
    world.svc.record_purchase(
        world.emp("blr", 4),
        [{"refill_id": refill["id"], "amount_minor": 100_000}],  # 1 000 INR
        "INR",
        "receipts/snapshot.jpg",
        allow_rescue=True,
    )

    world.db.conn.execute(
        "INSERT OR REPLACE INTO fx_rates (pair, rate, as_of, source) VALUES ('INR>NOK', '0.5', '2026-11-20', 'test')"
    )
    world.db.conn.execute(
        "INSERT OR REPLACE INTO fx_rates (pair, rate, as_of, source) VALUES ('INR>NOK', '2.0', '2026-12-01', 'later')"
    )

    from snackcounter.report import monthly_report

    november = monthly_report(world.db, period="2026-11")
    assert november["grand_total"]["minor"] == convert(
        Money(100_000, Currency.INR), Currency.NOK, Decimal("0.5")
    ).minor == 50_000
