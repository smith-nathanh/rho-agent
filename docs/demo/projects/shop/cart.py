"""A shopping cart."""

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(frozen=True)
class Line:
    name: str
    unit_price: Decimal
    quantity: int
    category: str = "general"

    @property
    def amount(self) -> Decimal:
        return self.unit_price * self.quantity


@dataclass
class Cart:
    lines: list[Line] = field(default_factory=list)
    coupon_percent: Decimal = Decimal("0")

    def add(self, name: str, unit_price: str, quantity: int = 1, category: str = "general") -> None:
        self.lines.append(Line(name, Decimal(unit_price), quantity, category))

    @property
    def subtotal(self) -> Decimal:
        return sum((line.amount for line in self.lines), Decimal("0.00"))
