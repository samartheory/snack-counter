from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from .clock import SystemClock, local_date, utc_iso
from .config import Settings
from .db import init_db
from .errors import DomainError
from .report import monthly_report
from .seed import is_empty, reset, seed
from .service import SnackService

STATIC_DIR = Path(__file__).parent / "static"


class ReportIn(BaseModel):
    actor_id: str
    note: str | None = None


class EmployeeIn(BaseModel):
    employee_id: str


class DeclineIn(EmployeeIn):
    reason: str | None = None


class CancelIn(BaseModel):
    actor_id: str
    reason: str


class PurchaseLineIn(BaseModel):
    refill_id: str
    amount_minor: int


class PurchaseIn(BaseModel):
    buyer_id: str
    currency: str
    receipt_ref: str
    lines: list[PurchaseLineIn]
    allow_rescue: bool = False


class SweepIn(BaseModel):
    minutes: float = 0


class OverrideIn(BaseModel):
    office_id: str
    actor_id: str
    reason: str


class PresenceIn(BaseModel):
    employee_id: str
    on_date: str
    status: str
    office_id: str | None = None


def create_app(
    db_path: str = ":memory:",
    *,
    clock=None,
    settings: Settings | None = None,
    seed_data: bool = True,
) -> FastAPI:
    clock = clock or SystemClock()
    settings = settings or Settings()
    db = init_db(db_path)
    if seed_data and is_empty(db):
        with db.tx():
            seed(db, clock)
    service = SnackService(db, clock=clock, settings=settings)

    app = FastAPI(title="Snack Counter", version="0.1.0")
    app.state.db = db
    app.state.svc = service

    @app.exception_handler(DomainError)
    async def domain_error_handler(_request, exc: DomainError):
        return JSONResponse(status_code=exc.http_status, content=exc.to_dict())

    @app.get("/api/health")
    def health():
        return {"ok": True, "now": utc_iso(service.clock.now())}

    @app.get("/api/bootstrap")
    def bootstrap(period: str | None = None):
        return service.bootstrap(period)

    @app.post("/api/boxes/{box_id}/report")
    def report_empty(box_id: str, body: ReportIn):
        return service.report_empty(box_id, body.actor_id, body.note)

    @app.post("/api/refills/{refill_id}/accept")
    def accept(refill_id: str, body: EmployeeIn):
        return service.accept(refill_id, body.employee_id)

    @app.post("/api/refills/{refill_id}/decline")
    def decline(refill_id: str, body: DeclineIn):
        return service.decline(refill_id, body.employee_id, body.reason)

    @app.post("/api/refills/{refill_id}/still-on-it")
    def still_on_it(refill_id: str, body: EmployeeIn):
        return service.mark_still_on_it(refill_id, body.employee_id)

    @app.post("/api/refills/{refill_id}/cancel")
    def cancel(refill_id: str, body: CancelIn):
        return service.cancel(refill_id, body.actor_id, body.reason)

    @app.post("/api/refills/{refill_id}/reopen")
    def reopen(refill_id: str, body: EmployeeIn):
        return service.reopen(refill_id, body.employee_id)

    @app.post("/api/purchases")
    def purchase(body: PurchaseIn):
        return service.record_purchase(
            body.buyer_id,
            [line.model_dump() for line in body.lines],
            body.currency,
            body.receipt_ref,
            allow_rescue=body.allow_rescue,
        )

    @app.post("/api/sweep")
    def sweep(body: SweepIn):
        if body.minutes and hasattr(service.clock, "advance"):
            service.clock.advance(minutes=body.minutes)
        return service.sweep()

    @app.post("/api/budget/override")
    def override(body: OverrideIn):
        return service.override_budget(body.office_id, body.actor_id, body.reason)

    @app.post("/api/presence")
    def presence(body: PresenceIn):
        service.set_presence(body.employee_id, body.on_date, body.status, body.office_id)
        return service.bootstrap()

    @app.get("/api/report")
    def report(
        period: str | None = Query(default=None),
        reporting_currency: str | None = Query(default=None),
    ):
        if period is None:
            offices = service.bootstrap()["offices"]
            period = local_date(service.clock.now(), offices[0]["timezone"]).strftime("%Y-%m") if offices else datetime.now().strftime("%Y-%m")
        return monthly_report(
            db,
            period=period,
            reporting_currency=reporting_currency or settings.reporting_currency,
        )

    @app.post("/api/reset")
    def reset_demo():
        with db.tx():
            reset(db)
            seed(db, service.clock)
        return service.bootstrap()

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    return app


app = create_app()
