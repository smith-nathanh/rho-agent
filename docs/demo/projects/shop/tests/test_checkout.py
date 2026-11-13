from decimal import Decimal

from cart import Cart
from checkout import checkout


def test_books_are_not_taxed():
    cart = Cart()
    cart.add("novel", "12.00", category="books")
    assert checkout(cart).total == Decimal("12.00")


def test_general_goods_are_taxed():
    cart = Cart()
    cart.add("lamp", "20.00")
    receipt = checkout(cart)
    assert receipt.tax == Decimal("1.60")
    assert receipt.total == Decimal("21.60")


def test_food_tax_on_a_single_item():
    cart = Cart()
    cart.add("sandwich", "8.50", category="food")
    receipt = checkout(cart)
    assert receipt.tax == Decimal("0.43")
    assert receipt.total == Decimal("8.93")


def test_coupon_comes_off_the_subtotal():
    cart = Cart(coupon_percent=Decimal("10"))
    cart.add("mug", "12.25", category="books")
    receipt = checkout(cart)
    assert receipt.discount == Decimal("1.23")
    assert receipt.total == Decimal("11.02")


def test_mixed_cart():
    cart = Cart(coupon_percent=Decimal("20"))
    cart.add("coffee", "9.00", quantity=2, category="food")
    cart.add("lamp", "20.00")
    receipt = checkout(cart)
    assert receipt.subtotal == Decimal("38.00")
    assert receipt.discount == Decimal("7.60")
    assert receipt.tax == Decimal("2.50")
    assert receipt.total == Decimal("32.90")
