from __future__ import annotations

from datetime import timedelta

from .clock import Clock, local_date, utc_iso

OSLO_NAMES = [
    "Anna Solberg", "Bjørn Haugen", "Clara Nilsen", "Daniel Berg", "Eva Lund",
    "Fredrik Dahl", "Guro Moen", "Henrik Aas", "Ingrid Vik", "Jonas Holt",
    "Kari Sund", "Lars Ek", "Mari Ruud", "Nils Fjell", "Oda Strand",
]
BENGALURU_NAMES = [
    "Priya Menon", "Rahul Iyer", "Meera Nair", "Arjun Rao", "Kavya Reddy",
    "Vikram Shetty", "Ananya Bose", "Siddharth Jain", "Divya Pillai", "Karthik Raman",
    "Neha Gupta", "Aditya Kulkarni", "Sneha Patil", "Rohan Das", "Isha Verma",
    "Manish Tiwari", "Pooja Hegde", "Nikhil Bhat", "Ritu Chopra", "Sameer Khan",
]

OFFICES = {
    "osl": {
        "name": "Oslo",
        "currency": "NOK",
        "budget": 600_000,  # 6 000 NOK in øre
        "typical_min": 30_000,
        "typical_max": 120_000,
        "timezone": "Europe/Oslo",
        "names": OSLO_NAMES,
        "boxes": [
            ("Almonds & nuts", "nuts"),
            ("Fruit & bars", "fruit"),
            ("Chocolate", "sweet"),
        ],
    },
    "blr": {
        "name": "Bengaluru",
        "currency": "INR",
        "budget": 1_500_000,  # 15 000 INR in paise
        "typical_min": 50_000,
        "typical_max": 300_000,
        "timezone": "Asia/Kolkata",
        "names": BENGALURU_NAMES,
        "boxes": [
            ("Namkeen", "savoury"),
            ("Biscuits", "biscuits"),
            ("Fresh fruit", "fruit"),
        ],
    },
}


def is_empty(db) -> bool:
    return db.conn.execute("SELECT COUNT(*) FROM offices").fetchone()[0] == 0


def reset(db) -> None:
    # Deletion order matters: children before parents, because FKs are on.
    tables = [
        "messages", "audit", "outbox", "hr_claims", "claims", "purchase_lines",
        "budget_entries", "budget_overrides", "refill_exclusions", "offers",
        "refills", "purchases", "boxes", "presence", "fairness", "employees",
        "offices", "fx_rates",
    ]
    for table in tables:
        db.conn.execute(f"DELETE FROM {table}")


def seed(db, clock: Clock) -> dict:
    """Two offices, 35 employees, six boxes, and a fresh FX snapshot."""
    now = clock.now()
    if not is_empty(db):
        return existing_ids(db)

    conn = db.conn
    directory: dict[str, dict] = {}

    for office_id, spec in OFFICES.items():
        conn.execute(
            "INSERT INTO offices (id, name, currency, monthly_budget_minor, typical_min_minor,"
            " typical_max_minor, timezone, holidays) VALUES (?, ?, ?, ?, ?, ?, ?, '[]')",
            (
                office_id,
                spec["name"],
                spec["currency"],
                spec["budget"],
                spec["typical_min"],
                spec["typical_max"],
                spec["timezone"],
            ),
        )

        employee_ids: list[str] = []
        first_local = local_date(now, spec["timezone"])
        for index, name in enumerate(spec["names"], start=1):
            employee_id = f"{office_id}-{index:02d}"
            employee_ids.append(employee_id)
            conn.execute(
                "INSERT INTO employees (id, name, home_office_id, hr_employee_id,"
                " payroll_currency, is_volunteer) VALUES (?, ?, ?, ?, ?, 1)",
                (
                    employee_id,
                    name,
                    office_id,
                    f"HR-{office_id.upper()}-{index:02d}",
                    spec["currency"],
                ),
            )
            for offset in range(0, 21):
                day = first_local + timedelta(days=offset)
                status = "office"
                if (index + offset) % 9 == 0:
                    status = "remote"
                elif (index + offset) % 13 == 0:
                    status = "leave"
                conn.execute(
                    "INSERT INTO presence (employee_id, on_date, office_id, status)"
                    " VALUES (?, ?, ?, ?)",
                    (employee_id, day.isoformat(), office_id, status),
                )

        box_ids: dict[str, str] = {}
        for index, (label, category) in enumerate(spec["boxes"], start=1):
            box_id = f"{office_id}-box-{index}"
            box_ids[label] = box_id
            conn.execute(
                "INSERT INTO boxes (id, office_id, label, category, status)"
                " VALUES (?, ?, ?, ?, 'stocked')",
                (box_id, office_id, label, category),
            )

        manager_id = employee_ids[0]
        conn.execute(
            "UPDATE offices SET manager_employee_id = ? WHERE id = ?", (manager_id, office_id)
        )
        directory[office_id] = {
            "id": office_id,
            "name": spec["name"],
            "currency": spec["currency"],
            "timezone": spec["timezone"],
            "manager_id": manager_id,
            "employee_ids": employee_ids,
            "boxes": box_ids,
        }

    rates = [
        ("NOK>INR", "8.30", "2020-01-01", "Norges Bank (seed)"),
        ("INR>NOK", "0.12048", "2020-01-01", "Norges Bank (seed)"),
        ("NOK>INR", "8.42", "2026-11-01", "Norges Bank"),
        ("INR>NOK", "0.11876", "2026-11-01", "Norges Bank"),
    ]
    for pair, rate, as_of, source in rates:
        conn.execute(
            "INSERT OR REPLACE INTO fx_rates (pair, rate, as_of, source) VALUES (?, ?, ?, ?)",
            (pair, rate, as_of, source),
        )

    conn.execute(
        "INSERT INTO messages (id, at, channel, kind, text) VALUES"
        " ('seed-1', ?, '#general', 'info', 'Snack Counter is live. Tap a box to report it empty.')",
        (utc_iso(now),),
    )
    return directory


def existing_ids(db) -> dict:
    office_rows = db.conn.execute("SELECT * FROM offices").fetchall()
    directory: dict[str, dict] = {}
    for office in office_rows:
        employees = [
            row[0]
            for row in db.conn.execute(
                "SELECT id FROM employees WHERE home_office_id = ? ORDER BY id", (office["id"],)
            ).fetchall()
        ]
        boxes = {
            row["label"]: row["id"]
            for row in db.conn.execute(
                "SELECT id, label FROM boxes WHERE office_id = ?", (office["id"],)
            ).fetchall()
        }
        directory[office["id"]] = {
            "id": office["id"],
            "name": office["name"],
            "currency": office["currency"],
            "timezone": office["timezone"],
            "manager_id": office["manager_employee_id"],
            "employee_ids": employees,
            "boxes": boxes,
        }
    return directory
