"""Money is always integer minor units plus a currency. No floats, ever.

NOK -> øre, INR -> paise. Both have 100 subunits, so the arithmetic is uniform,
but the type still refuses to mix currencies by accident.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum


class Currency(str, Enum):
    NOK = "NOK"
    INR = "INR"


@dataclass(frozen=True)
class Money:
    minor: int
    currency: Currency

    def __post_init__(self) -> None:
        if isinstance(self.minor, bool) or not isinstance(self.minor, int):
            raise TypeError("Money.minor must be an int (minor units)")
        object.__setattr__(self, "currency", Currency(self.currency))

    # -- construction -----------------------------------------------------
    @classmethod
    def from_major(cls, amount: Decimal | str | int, currency: Currency | str) -> "Money":
        value = (Decimal(str(amount)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        return cls(int(value), Currency(currency))

    @classmethod
    def zero(cls, currency: Currency | str) -> "Money":
        return cls(0, Currency(currency))

    # -- accessors --------------------------------------------------------
    @property
    def major(self) -> Decimal:
        return (Decimal(self.minor) / 100).quantize(Decimal("0.01"))

    def __str__(self) -> str:
        return f"{self.major:,.2f} {self.currency.value}"

    def to_dict(self) -> dict:
        return {"minor": self.minor, "currency": self.currency.value, "display": str(self)}

    # -- arithmetic -------------------------------------------------------
    def _require(self, other: "Money") -> None:
        if self.currency != other.currency:
            raise ValueError(f"currency mismatch: {self.currency} vs {other.currency}")

    def __add__(self, other: "Money") -> "Money":
        self._require(other)
        return Money(self.minor + other.minor, self.currency)

    def __sub__(self, other: "Money") -> "Money":
        self._require(other)
        return Money(self.minor - other.minor, self.currency)

    def __neg__(self) -> "Money":
        return Money(-self.minor, self.currency)

    def __mul__(self, factor: Decimal) -> "Money":
        return Money(
            int((Decimal(self.minor) * factor).quantize(Decimal("1"), rounding=ROUND_HALF_UP)),
            self.currency,
        )

    def is_negative(self) -> bool:
        return self.minor < 0


def convert(amount: Money, target: Currency | str, rate: Decimal | str) -> Money:
    """Convert with an explicit rate. The caller is responsible for storing the rate it used."""
    target = Currency(target)
    if amount.currency == target:
        return amount
    rate_d = Decimal(str(rate))
    if rate_d <= 0:
        raise ValueError("FX rate must be positive")
    return Money(
        int((Decimal(amount.minor) * rate_d).quantize(Decimal("1"), rounding=ROUND_HALF_UP)),
        target,
    )


def sum_money(values: list[Money], currency: Currency | str) -> Money:
    total = Money.zero(currency)
    for value in values:
        total = total + value
    return total


def allocate(total: Money, weights: list[int]) -> list[Money]:
    """Split a receipt across boxes using largest-remainder so the parts sum exactly to the total."""
    if not weights or any(w < 0 for w in weights):
        raise ValueError("weights must be a non-empty list of non-negative ints")
    weight_sum = sum(weights)
    if weight_sum == 0:
        weights = [1] * len(weights)
        weight_sum = len(weights)
    raw = [Decimal(total.minor) * Decimal(w) / Decimal(weight_sum) for w in weights]
    floors = [int(r) for r in raw]
    remainder = total.minor - sum(floors)
    order = sorted(range(len(raw)), key=lambda i: (raw[i] - floors[i]), reverse=True)
    for i in order[:remainder]:
        floors[i] += 1
    return [Money(v, total.currency) for v in floors]
