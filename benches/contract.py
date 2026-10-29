"""Messages shared by the benchmarks: small, medium (20 fields, nested) and large.

`make_models()` returns fresh classes: fastgrpc2 writes attributes on the classes it
registers, so each library gets its own copies. Field numbers follow declaration order in
both libraries.
"""

import datetime
import enum
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

__all__ = ["Models", "make_models", "sample"]

SIZES = ("small", "medium", "large")


@dataclass(frozen=True)
class Models:
    small: type[BaseModel]
    medium: type[BaseModel]
    large: type[BaseModel]
    line: type[BaseModel]
    address: type[BaseModel]
    status: Any

    def of(self, size: str) -> type[BaseModel]:
        model: type[BaseModel] = getattr(self, size)
        return model


def make_models() -> Models:
    """Fresh model classes (see the module docstring)."""

    class Status(enum.IntEnum):
        UNKNOWN = 0
        PAID = 1
        SHIPPED = 2

    class Small(BaseModel):
        id: int = 0
        name: str = ""

    class Address(BaseModel):
        street: str = ""
        city: str = ""
        zip: str = ""
        country: str = ""

    class Line(BaseModel):
        sku: str = ""
        qty: int = 0
        price: float = 0.0
        note: str | None = None

    class Order(BaseModel):
        order_number: str = ""
        customer: str = ""
        email: str = ""
        status: Status = Status.UNKNOWN
        total: float = 0.0
        currency: str = ""
        paid: bool = False
        items: int = 0
        created_at: datetime.datetime | None = None
        address: Address | None = None
        billing: Address | None = None
        lines: list[Line] = []
        tags: list[str] = []
        priority: int = 0
        weight: float = 0.0
        gift: bool = False
        message: str = ""
        channel: str = ""
        discount: float = 0.0
        reference: str = ""

    class Catalog(BaseModel):
        lines: list[Line] = []

    return Models(Small, Order, Catalog, Line, Address, Status)


def sample(models: Models, size: str) -> BaseModel:
    """A filled instance of the message of `size`."""
    line = models.line
    if size == "small":
        return models.small(id=42, name="bench")
    if size == "large":
        return models.large(
            lines=[line(sku=f"SKU-{i:05d}", qty=i % 7, price=i * 0.5) for i in range(1000)]
        )
    address = models.address(street="1 rue de la Paix", city="Paris", zip="75002", country="FR")
    return models.medium(
        order_number="ORD-2026-000123",
        customer="Ada Lovelace",
        email="ada@example.com",
        status=models.status.PAID,
        total=1234.5,
        currency="EUR",
        paid=True,
        items=3,
        created_at=datetime.datetime(2026, 10, 29, 12, 0, tzinfo=datetime.UTC),
        address=address,
        billing=address,
        lines=[
            line(sku="A-1", qty=1, price=10.0, note="gift wrap"),
            line(sku="B-2", qty=2, price=20.5),
            line(sku="C-3", qty=3, price=30.25),
        ],
        tags=["vip", "express"],
        priority=2,
        weight=3.75,
        gift=True,
        message="Happy birthday!",
        channel="web",
        discount=5.0,
        reference="REF-9",
    )
