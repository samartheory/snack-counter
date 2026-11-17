from __future__ import annotations

import calendar
from datetime import date

from .db import many, one
from .fx import lookup_rate
from .money import Currency, Money, convert


def _month_end(period: str) -> str:
    year, month = (int(part) for part in period.split("-"))
    last = calendar.monthrange(year, month)[1]
    return date(year, month, last).isoformat()


def monthly_report(
    db,
    *,
    period: str,
    reporting_currency: str = "NOK",
) -> dict:
    """Spend for one calendar month, attributed to the purchase date in the office's timezone.

    Each office is reported in its own currency. The grand total is the only place
    currencies meet, and it uses an explicit, persisted rate snapshot.
    """
    conn = db.conn
    reporting_currency = Currency(reporting_currency).value
    as_of = _month_end(period)

    offices = many(conn, "SELECT * FROM offices ORDER BY name")
    spent_rows = many(
        conn,
        """
        SELECT office_id,
               COALESCE(SUM(amount_office_minor), 0) AS spent_minor,
               COUNT(DISTINCT purchase_id) AS purchases,
               COUNT(*) AS refills
          FROM purchase_lines WHERE period = ? GROUP BY office_id
        """,
        (period,),
    )
    spent_by_office = {row["office_id"]: row for row in spent_rows}

    budget_rows = many(
        conn,
        """
        SELECT office_id,
               COALESCE(SUM(CASE WHEN kind = 'reserve' THEN amount_minor
                                 WHEN kind = 'release' THEN -amount_minor ELSE 0 END), 0) AS reserved_minor
          FROM budget_entries WHERE period = ? GROUP BY office_id
        """,
        (period,),
    )
    reserved_by_office = {row["office_id"]: row["reserved_minor"] for row in budget_rows}

    outstanding_rows = many(
        conn,
        """
        SELECT l.office_id, c.currency, SUM(l.amount_minor) AS amount_minor, COUNT(DISTINCT c.id) AS claims
          FROM claims c JOIN purchase_lines l ON l.purchase_id = c.purchase_id
         WHERE l.period = ? AND c.status = 'PENDING'
         GROUP BY l.office_id, c.currency
        """,
        (period,),
    )

    office_reports = []
    for office in offices:
        spent = spent_by_office.get(office["id"], {})
        categories = many(
            conn,
            """
            SELECT b.category,
                   SUM(l.amount_office_minor) AS amount_minor,
                   COUNT(*) AS refills
              FROM purchase_lines l
              JOIN refills r ON r.id = l.refill_id
              JOIN boxes b ON b.id = r.box_id
             WHERE l.period = ? AND l.office_id = ?
             GROUP BY b.category ORDER BY amount_minor DESC
            """,
            (period, office["id"]),
        )
        outstanding = [
            {
                "currency": row["currency"],
                "amount_minor": row["amount_minor"],
                "claims": row["claims"],
            }
            for row in outstanding_rows
            if row["office_id"] == office["id"]
        ]
        spent_minor = int(spent.get("spent_minor", 0) or 0)
        budget = office["monthly_budget_minor"]
        reserved = int(reserved_by_office.get(office["id"], 0) or 0)
        office_reports.append(
            {
                "office_id": office["id"],
                "office_name": office["name"],
                "currency": office["currency"],
                "budget_minor": budget,
                "spent_minor": spent_minor,
                "reserved_minor": reserved,
                "remaining_minor": budget - spent_minor - reserved,
                "spent_display": str(Money(spent_minor, office["currency"])),
                "budget_display": str(Money(budget, office["currency"])),
                "purchases": int(spent.get("purchases", 0) or 0),
                "boxes_refilled": int(spent.get("refills", 0) or 0),
                "by_category": [
                    {
                        "category": row["category"],
                        "amount_minor": row["amount_minor"],
                        "display": str(Money(row["amount_minor"], office["currency"])),
                        "refills": row["refills"],
                    }
                    for row in categories
                ],
                "outstanding_claims": outstanding,
            }
        )

    # Per-currency subtotals: never add NOK to INR.
    by_currency: dict[str, int] = {}
    for office in office_reports:
        by_currency[office["currency"]] = by_currency.get(office["currency"], 0) + office["spent_minor"]

    grand_minor = 0
    rates: list[dict] = []
    unconverted: list[dict] = []
    for currency, minor in sorted(by_currency.items()):
        if minor == 0:
            continue
        if currency == reporting_currency:
            grand_minor += minor
            continue
        try:
            rate_text, source = lookup_rate(conn, currency, reporting_currency, as_of)
        except Exception:
            unconverted.append(
                {"currency": currency, "amount_minor": minor, "display": str(Money(minor, currency))}
            )
            continue
        converted = convert(Money(minor, currency), reporting_currency, rate_text)
        grand_minor += converted.minor
        rates.append(
            {
                "from": currency,
                "to": reporting_currency,
                "rate": rate_text,
                "as_of": as_of,
                "source": source,
                "amount_minor": minor,
                "converted_minor": converted.minor,
            }
        )

    outstanding_by_currency: dict[str, dict] = {}
    for row in outstanding_rows:
        bucket = outstanding_by_currency.setdefault(
            row["currency"], {"minor": 0, "claims": 0}
        )
        bucket["minor"] += int(row["amount_minor"] or 0)
        bucket["claims"] += int(row["claims"] or 0)

    return {
        "period": period,
        "reporting_currency": reporting_currency,
        "rate_as_of": as_of,
        "offices": office_reports,
        "totals_by_currency": [
            {
                "currency": currency,
                "minor": minor,
                "display": str(Money(minor, currency)),
            }
            for currency, minor in sorted(by_currency.items())
        ],
        "grand_total": {
            "minor": grand_minor,
            "currency": reporting_currency,
            "display": str(Money(grand_minor, reporting_currency)),
            "rates": rates,
            "unconverted": unconverted,
        },
        "outstanding_by_currency": [
            {
                "currency": currency,
                "minor": value["minor"],
                "display": str(Money(value["minor"], currency)),
                "claims": value["claims"],
            }
            for currency, value in sorted(outstanding_by_currency.items())
        ],
        "purchase_count": int(
            one(
                conn,
                "SELECT COUNT(DISTINCT purchase_id) AS n FROM purchase_lines WHERE period = ?",
                (period,),
            )["n"]
            or 0
        ),
    }
