"""Turn a cart into a receipt."""

from dataclasses import dataclass
from decimal import Decimal

from cart import Cart
from money import percent_of
from tax import tax_for


@dataclass(frozen=True)
class Receipt:
    subtotal: Decimal
    discount: Decimal
    tax: Decimal

    @property
    def total(self) -> Decimal:
        return self.subtotal - self.discount + self.tax


def checkout(cart: Cart) -> Receipt:
    """Price a cart: the coupon comes off the subtotal, then each line is taxed."""
    discount = percent_of(cart.subtotal, cart.coupon_percent)
    tax = sum((tax_for(line.amount, line.category) for line in cart.lines), Decimal("0.00"))
    return Receipt(subtotal=cart.subtotal, discount=discount, tax=tax)
