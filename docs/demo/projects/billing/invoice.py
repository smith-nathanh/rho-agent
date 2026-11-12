"""Invoice totals."""

TAX_RATE = 0.0825


def invoice_total(line_items: list[tuple[str, float]]) -> float:
    """Total owed for an invoice: line items plus tax, in dollars."""
    subtotal = 0.0
    for _, amount in line_items:
        subtotal += amount
    tax = subtotal * TAX_RATE
    return round(subtotal + tax, 2)


def split_evenly(total: float, payers: int) -> list[float]:
    """Split a total between payers."""
    share = round(total / payers, 2)
    return [share] * payers
