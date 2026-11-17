from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from .api import create_app
from .clock import FakeClock, fmt_local, parse_iso
from .config import Settings
from .db import init_db, many
from .errors import Conflict, DomainError
from .money import Money
from .report import monthly_report
from .seed import is_empty, seed
from .service import SnackService


def _tz_for_channel(channel: str) -> str:
    return "Asia/Kolkata" if "bengaluru" in channel else "Europe/Oslo"


def _fmt_report(report: dict) -> str:
    lines = []
    lines.append(
        f"Monthly snack report — {report['period']} "
        f"(FX snapshot {report['rate_as_of']}, reporting in {report['reporting_currency']})"
    )
    lines.append("")
    for office in report["offices"]:
        cur = office["currency"]
        lines.append(
            f"  {office['office_name']:<10} {Money(office['spent_minor'], cur)!s:>16} spent"
            f" / {Money(office['budget_minor'], cur)} budget"
            f"   ({office['purchases']} purchases, {office['boxes_refilled']} boxes)"
        )
        for cat in office["by_category"]:
            lines.append(f"      {cat['category']:<12} {Money(cat['amount_minor'], cur)}")
    lines.append("")
    lines.append("  Subtotals: " + "  |  ".join(
        f"{t['currency']} {t['minor'] / 100:,.2f}" for t in report["totals_by_currency"]
    ))
    total = report["grand_total"]
    lines.append(
        f"  Grand total: {Money(total['minor'], total['currency'])}"
    )
    for rate in total["rates"]:
        lines.append(
            f"      {rate['from']}→{rate['to']} @ {rate['rate']} ({rate['as_of']}, {rate['source']})"
        )
    if total["unconverted"]:
        lines.append("      NOT converted: " + ", ".join(
            f"{u['currency']} {Money(u['amount_minor'], u['currency'])}" for u in total["unconverted"]
        ))
    outstanding = report["outstanding_by_currency"]
    lines.append(
        "  Outstanding claims: "
        + ("  |  ".join(f"{o['currency']} {Money(o['minor'], o['currency'])} ({o['claims']})" for o in outstanding) or "none")
    )
    return "\n".join(lines)


class Demo:
    def __init__(self) -> None:
        self.clock = FakeClock(datetime(2026, 11, 16, 8, 0, tzinfo=timezone.utc))  # 09:00 Oslo
        self.db = init_db(":memory:")
        with self.db.tx():
            self.directory = seed(self.db, self.clock)
        self.svc = SnackService(self.db, clock=self.clock, settings=Settings())
        self.seen = 0

    # -- output -----------------------------------------------------------
    def section(self, title: str) -> None:
        print(f"\n\033[1m{title}\033[0m")
        print("─" * min(100, len(title) + 8))

    def flush(self) -> None:
        rows = many(
            self.db.conn,
            "SELECT * FROM messages ORDER BY rowid LIMIT -1 OFFSET ?",
            (self.seen,),
        )
        for row in rows:
            tz = _tz_for_channel(row["channel"])
            stamp = fmt_local(parse_iso(row["at"]), tz)
            print(f"  \033[2m{stamp}\033[0m {row['channel']:<20} {row['text']}")
        self.seen += len(rows)

    def move(self, when: datetime, label: str) -> None:
        self.clock.set(when)
        print(f"\n  \033[2m⋯ clock → {label}\033[0m")

    def offered(self, refill_id: str) -> str:
        view = self.svc.refill_view(refill_id)
        return view["offered_to_id"] or view["assignee_id"]

    def report(self, office_key: str, box_label: str, actor_index: int) -> dict:
        office = self.directory[office_key]
        refill = self.svc.report_empty(
            office["boxes"][box_label], office["employee_ids"][actor_index]
        )
        return refill

    def run(self) -> None:
        oslo = self.directory["osl"]

        self.section("1. Monday 09:10 — a box is empty, someone commits")
        self.move(datetime(2026, 11, 16, 8, 10, tzinfo=timezone.utc), "Mon 09:10 Oslo")
        almonds = self.report("osl", "Almonds & nuts", 4)
        self.flush()
        offered_to = almonds["offered_to_id"]
        print(f"  → offered to {almonds['offered_to_name']} (fewest recent refills, on site)")
        self.svc.accept(almonds["id"], offered_to)
        self.flush()

        self.section("2. Monday 09:12 — a second person taps 'accept'")
        other = next(e for e in oslo["employee_ids"] if e != offered_to)
        try:
            self.svc.accept(almonds["id"], other)
            print("  → ERROR: two people committed to one refill")
        except Conflict as exc:
            print(f"  → rejected for the second person: {exc.message}")

        self.section("3. Monday 14:20 — receipt logged, claim created")
        self.move(datetime(2026, 11, 16, 13, 20, tzinfo=timezone.utc), "Mon 14:20 Oslo")
        self.svc.record_purchase(
            offered_to,
            [{"refill_id": almonds["id"], "amount_minor": 64_000}],
            "NOK",
            "receipts/almonds-2026-11-16.jpg",
        )
        self.flush()

        self.section("4. Monday 16:30 — one trip, two boxes, one receipt")
        self.move(datetime(2026, 11, 16, 15, 30, tzinfo=timezone.utc), "Mon 16:30 Oslo")
        fruit = self.report("osl", "Fruit & bars", 8)
        choc = self.report("osl", "Chocolate", 9)
        self.flush()
        print("  → both boxes still unclaimed; a volunteer at the shop rescues them in one trip")
        self.svc.record_purchase(
            oslo["employee_ids"][13],
            [
                {"refill_id": fruit["id"], "amount_minor": 44_500},
                {"refill_id": choc["id"], "amount_minor": 44_500},
            ],
            "NOK",
            "receipts/shop-trip-2026-11-16.jpg",
            allow_rescue=True,
        )
        self.flush()

        self.section("5. Tuesday 10:00 Bengaluru — decline ladder")
        self.move(datetime(2026, 11, 17, 4, 30, tzinfo=timezone.utc), "Tue 10:00 Bengaluru")
        biscuits = self.report("blr", "Biscuits", 2)
        self.flush()
        first = biscuits["offered_to_id"]
        self.svc.decline(biscuits["id"], first, "Not at the office today")
        self.flush()
        biscuits = self.svc.refill_view(biscuits["id"])
        print(f"  → {biscuits['offered_to_name']} is next in line")
        self.svc.accept(biscuits["id"], biscuits["offered_to_id"])
        self.svc.record_purchase(
            biscuits["offered_to_id"],
            [{"refill_id": biscuits["id"], "amount_minor": 145_000}],
            "INR",
            "receipts/biscuits-2026-11-17.jpg",
        )
        self.flush()

        self.section("6. Tuesday 11:00 Bengaluru — accepted, then goes quiet")
        self.move(datetime(2026, 11, 17, 5, 30, tzinfo=timezone.utc), "Tue 11:00 Bengaluru")
        namkeen = self.report("blr", "Namkeen", 5)
        self.flush()
        quiet = namkeen["offered_to_id"]
        self.svc.accept(namkeen["id"], quiet)
        self.flush()

        self.section("7. Wednesday 19:00 — deadline missed, reminder sent")
        self.move(datetime(2026, 11, 18, 13, 30, tzinfo=timezone.utc), "Wed 19:00 Bengaluru")
        self.svc.sweep()
        self.flush()

        self.section("8. Wednesday 23:00 — still nothing, handed to someone else")
        self.move(datetime(2026, 11, 18, 17, 30, tzinfo=timezone.utc), "Wed 23:00 Bengaluru")
        self.svc.sweep()
        self.flush()
        namkeen = self.svc.refill_view(namkeen["id"])
        replacement = namkeen["offered_to_id"]
        self.svc.accept(namkeen["id"], replacement)
        self.svc.record_purchase(
            replacement,
            [{"refill_id": namkeen["id"], "amount_minor": 80_000}],
            "INR",
            "receipts/namkeen-2026-11-19.jpg",
        )
        self.flush()

        self.section("9. HR decisions arrive (1–10 days, polled by the sweeper)")
        for day in range(20, 31):
            self.move(
                datetime(2026, 11, day, 10, 0, tzinfo=timezone.utc),
                f"Nov {day:02d} 11:00 Oslo",
            )
            result = self.svc.sweep()
            if result["claim_decisions"]:
                self.flush()

        self.section("10. Monthly report")
        report = monthly_report(self.db, period="2026-11", reporting_currency="NOK")
        print(_fmt_report(report))

        print("\n\033[1mWhat just happened\033[0m")
        print("─" * 20)
        for refill in self.svc.list_refills(20):
            print(
                f"  {refill['box_label']:<16} {refill['office_name']:<10} "
                f"{refill['status']:<16} claim={refill['claim_status'] or '—':<10} "
                f"reserved={Money(refill['reserved_minor'], refill['currency'])}"
            )


def run_demo() -> None:
    Demo().run()


def run_report(db_path: str, period: str | None, currency: str) -> None:
    db = init_db(db_path)
    if is_empty(db):
        print("No data in that database yet. Run the demo or start the server first.")
        return
    if period is None:
        period = datetime.now(timezone.utc).strftime("%Y-%m")
    print(_fmt_report(monthly_report(db, period=period, reporting_currency=currency)))


def run_serve(db_path: str, port: int, reset: bool, demo_clock: bool) -> None:
    import uvicorn

    if reset:
        from .seed import reset as reset_db

        db = init_db(db_path)
        with db.tx():
            reset_db(db)
        db.close()

    clock = None
    if demo_clock:
        clock = FakeClock(datetime(2026, 11, 16, 8, 0, tzinfo=timezone.utc))  # 09:00 Oslo
    app = create_app(db_path, clock=clock)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="snackcounter", description="Snack counter automation")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("demo", help="replay a scripted week with a fake clock")

    serve = sub.add_parser("serve", help="run the API and dashboard")
    serve.add_argument("--db", default="snackcounter.db")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reset", action="store_true")
    serve.add_argument(
        "--demo-clock",
        action="store_true",
        help="use a simulated clock that the dashboard can advance (demo mode)",
    )

    report = sub.add_parser("report", help="print the monthly report")
    report.add_argument("--db", default="snackcounter.db")
    report.add_argument("--period", default=None, help="YYYY-MM")
    report.add_argument("--currency", default="NOK")

    args = parser.parse_args(argv)
    try:
        if args.command == "demo":
            run_demo()
        elif args.command == "report":
            run_report(args.db, args.period, args.currency)
        elif args.command == "serve":
            run_serve(args.db, args.port, args.reset, args.demo_clock)
    except DomainError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return 1
    return 0
