from __future__ import annotations

from fastapi.testclient import TestClient

from snackcounter.api import create_app
from snackcounter.clock import FakeClock
from conftest import START


def _client(tmp_path) -> TestClient:
    return TestClient(create_app(str(tmp_path / "api.sqlite"), clock=FakeClock(START)))


def test_bootstrap_and_dashboard(tmp_path) -> None:
    with _client(tmp_path) as client:
        assert client.get("/api/health").json()["ok"] is True
        boot = client.get("/api/bootstrap").json()
        assert len(boot["employees"]) == 35
        assert len(boot["boxes"]) == 6
        assert {office["name"] for office in boot["offices"]} == {"Oslo", "Bengaluru"}

        page = client.get("/")
        assert page.status_code == 200
        assert "Snack" in page.text


def test_report_accept_conflict_and_purchase(tmp_path) -> None:
    with _client(tmp_path) as client:
        boot = client.get("/api/bootstrap").json()
        actor = boot["employees"][0]["id"]
        box = next(b for b in boot["boxes"] if b["office_id"] == "osl")

        reported = client.post(f"/api/boxes/{box['id']}/report", json={"actor_id": actor})
        assert reported.status_code == 200
        refill = reported.json()
        assert refill["status"] == "OFFERED"
        assigned_to = refill["offered_to_id"]

        accepted = client.post(
            f"/api/refills/{refill['id']}/accept", json={"employee_id": assigned_to}
        )
        assert accepted.status_code == 200
        assert accepted.json()["status"] == "ASSIGNED"

        other = next(e["id"] for e in boot["employees"] if e["id"] != assigned_to)
        conflict = client.post(
            f"/api/refills/{refill['id']}/accept", json={"employee_id": other}
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"] == "conflict"

        purchase = client.post(
            "/api/purchases",
            json={
                "buyer_id": assigned_to,
                "currency": "NOK",
                "receipt_ref": "receipts/api.jpg",
                "lines": [{"refill_id": refill["id"], "amount_minor": 64_000}],
            },
        )
        assert purchase.status_code == 200
        assert purchase.json()["status"] == "PURCHASED"

        report = client.get("/api/report?period=2026-11").json()
        oslo = next(o for o in report["offices"] if o["office_id"] == "osl")
        assert oslo["spent_minor"] == 64_000
        assert report["grand_total"]["currency"] == "NOK"


def test_sweep_advances_the_fake_clock_and_settles_claims(tmp_path) -> None:
    with _client(tmp_path) as client:
        boot = client.get("/api/bootstrap").json()
        actor = boot["employees"][0]["id"]
        box = next(b for b in boot["boxes"] if b["office_id"] == "osl")
        refill = client.post(f"/api/boxes/{box['id']}/report", json={"actor_id": actor}).json()
        assigned_to = refill["offered_to_id"]
        client.post(f"/api/refills/{refill['id']}/accept", json={"employee_id": assigned_to})
        client.post(
            "/api/purchases",
            json={
                "buyer_id": assigned_to,
                "currency": "NOK",
                "receipt_ref": "receipts/sweep.jpg",
                "lines": [{"refill_id": refill["id"], "amount_minor": 40_000}],
            },
        )

        result = client.post("/api/sweep", json={"minutes": 60 * 24 * 12}).json()
        assert result["claim_decisions"] >= 1

        done = client.get("/api/bootstrap").json()["refills"][0]
        assert done["status"] == "REIMBURSED"


def test_validation_errors_are_reported(tmp_path) -> None:
    with _client(tmp_path) as client:
        response = client.post(
            "/api/purchases",
            json={"buyer_id": "nobody", "currency": "NOK", "receipt_ref": "", "lines": []},
        )
        assert response.status_code == 422
        assert response.json()["error"] == "validation_error"
