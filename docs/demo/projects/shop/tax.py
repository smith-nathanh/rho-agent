"""Sales tax by product category."""

from decimal import Decimal

from money import percent_of

RATES = {
    "food": Decimal("5"),
    "books": Decimal("0"),
    "general": Decimal("8"),
}


def tax_for(amount: Decimal, category: str) -> Decimal:
    """Tax due on `amount` of goods in `category`."""
    return percent_of(amount, RATES[category])
