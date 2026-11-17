from decimal import Decimal

import pytest

from snackcounter.money import Currency, Money, allocate, convert, sum_money


def test_money_is_integer_minor_units():
    with pytest.raises(TypeError):
        Money(12.5, Currency.NOK)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Money(True, Currency.NOK)  # type: ignore[arg-type]


def test_from_major_rounds_half_up():
    assert Money.from_major("123.456", "NOK").minor == 12_346
    assert Money.from_major("0.005", "NOK").minor == 1


def test_adding_different_currencies_is_refused():
    with pytest.raises(ValueError):
        Money(100, Currency.NOK) + Money(100, Currency.INR)


def test_conversion_uses_explicit_rate_and_rounds():
    inr = Money(150_000, Currency.INR)  # 1 500 INR
    nok = convert(inr, Currency.NOK, "0.11876")
    assert nok.currency == Currency.NOK
    assert nok.minor == 17_814  # 1500 * 0.11876 = 178.14


def test_conversion_same_currency_is_identity():
    money = Money(9_999, Currency.INR)
    assert convert(money, Currency.INR, "7") == money


def test_allocate_sums_exactly_to_the_receipt():
    total = Money(89_000, Currency.NOK)  # 890.00 NOK
    parts = allocate(total, [1, 1, 1])
    assert sum_money(parts, Currency.NOK).minor == total.minor
    assert len(parts) == 3

    weighted = allocate(total, [2, 1, 1])
    assert sum_money(weighted, Currency.NOK).minor == total.minor
    assert weighted[0].minor == 44_500


def test_allocate_rejects_negative_weights():
    with pytest.raises(ValueError):
        allocate(Money(100, Currency.NOK), [1, -1])


def test_display_and_dict():
    money = Money(64_000, Currency.NOK)
    assert str(money) == "640.00 NOK"
    assert money.to_dict()["currency"] == "NOK"
    assert money.major == Decimal("640.00")
