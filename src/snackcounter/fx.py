from __future__ import annotations

from decimal import Decimal

from .db import one
from .errors import ValidationError


def lookup_rate(conn, from_currency: str, to_currency: str, as_of: str) -> tuple[str, str]:
    """Latest known rate on or before `as_of`. Returns (rate_text, source)."""
    if from_currency == to_currency:
        return "1", "identity"
    row = one(
        conn,
        "SELECT rate, source FROM fx_rates WHERE pair = ? AND as_of <= ? ORDER BY as_of DESC LIMIT 1",
        (f"{from_currency}>{to_currency}", as_of),
    )
    if row is not None:
        return row["rate"], row["source"]
    reverse = one(
        conn,
        "SELECT rate, source FROM fx_rates WHERE pair = ? AND as_of <= ? ORDER BY as_of DESC LIMIT 1",
        (f"{to_currency}>{from_currency}", as_of),
    )
    if reverse is not None:
        inverted = (Decimal("1") / Decimal(reverse["rate"])).quantize(Decimal("0.00000001"))
        return str(inverted), reverse["source"] + " (inverted)"
    raise ValidationError(f"no FX rate for {from_currency}->{to_currency} on {as_of}")
