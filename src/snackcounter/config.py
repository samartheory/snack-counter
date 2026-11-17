from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """Tunable policy. Everything time-related here is an explicit choice, documented in docs/DESIGN.md."""

    # Coordination
    offer_window_minutes: int = 45
    offers_before_broadcast: int = 2
    purchase_due_business_days: int = 1
    due_hour_local: int = 18
    max_reminders: int = 1
    grace_hours: int = 4

    # Fairness / cash-flow protection for volunteers paying out of pocket
    max_open_claims: int = 2
    cooldown_days: int = 7

    # Reporting
    reporting_currency: str = "NOK"

    # A box purchased within this window cannot be reported empty again. This is the
    # last line of defence against "two people bought the same day".
    repeat_purchase_guard_hours: int = 6

    def __post_init__(self) -> None:
        if self.max_open_claims < 1:
            raise ValueError("max_open_claims must be >= 1")
        if self.offers_before_broadcast < 1:
            raise ValueError("offers_before_broadcast must be >= 1")
        if self.offer_window_minutes < 1 or self.grace_hours < 0:
            raise ValueError("invalid timing settings")
