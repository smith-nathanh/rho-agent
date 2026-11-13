"""Money helpers. All amounts are Decimal, in dollars."""

from decimal import Decimal

CENT = Decimal("0.01")


def round_cents(amount: Decimal) -> Decimal:
    """Round to whole cents. Half a cent rounds up, as on a printed receipt."""
    return amount.quantize(CENT)


def percent_of(amount: Decimal, percent: Decimal) -> Decimal:
    """`percent` percent of `amount`, rounded to cents."""
    return round_cents(amount * percent / Decimal("100"))
