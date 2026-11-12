"""Order pricing."""

from decimal import Decimal

BULK_THRESHOLD = 10
BULK_DISCOUNT = Decimal("0.10")


def line_total(unit_price: Decimal, quantity: int) -> Decimal:
    """Price of one order line. Orders of BULK_THRESHOLD or more get a discount."""
    total = unit_price * quantity
    if quantity > BULK_THRESHOLD:
        total -= total * BULK_DISCOUNT
    return total.quantize(Decimal("0.01"))


def order_total(lines: list[tuple[Decimal, int]]) -> Decimal:
    """Total for a list of (unit_price, quantity) lines."""
    return sum((line_total(price, qty) for price, qty in lines), Decimal("0.00"))
