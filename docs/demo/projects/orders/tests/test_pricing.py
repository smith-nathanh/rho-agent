from decimal import Decimal

from pricing import line_total, order_total


def test_small_order_pays_full_price():
    assert line_total(Decimal("4.00"), 3) == Decimal("12.00")


def test_bulk_discount_applies_at_threshold():
    assert line_total(Decimal("4.00"), 10) == Decimal("36.00")


def test_bulk_discount_applies_above_threshold():
    assert line_total(Decimal("4.00"), 20) == Decimal("72.00")


def test_order_total_sums_lines():
    lines = [(Decimal("4.00"), 3), (Decimal("2.50"), 10)]
    assert order_total(lines) == Decimal("34.50")
