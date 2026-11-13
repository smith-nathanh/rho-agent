from decimal import Decimal

from cart import Cart


def test_subtotal_adds_up_lines():
    cart = Cart()
    cart.add("coffee", "9.00", quantity=2, category="food")
    cart.add("notebook", "3.50")
    assert cart.subtotal == Decimal("21.50")


def test_empty_cart_is_free():
    assert Cart().subtotal == Decimal("0.00")
