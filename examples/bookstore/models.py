"""The messages of the bookstore contract (`bookstore.v1`).

Plain Pydantic models with a field number on each field: fastgrpc3 renders them as
`bookstore/v1/bookstore.proto` and compiles it when the server starts.
"""

import datetime

from fastgrpc3 import ProtoEnum, ProtoModel, field
from fastgrpc3.types import Int32, UInt32

PACKAGE = "bookstore.v1"


class Genre(ProtoEnum, proto="bookstore.v1.Genre"):
    """Kind of book. In the `.proto`: GENRE_UNSPECIFIED, GENRE_NOVEL…"""

    UNSPECIFIED = 0
    NOVEL = 1
    ESSAY = 2
    POETRY = 3


class Book(ProtoModel, package=PACKAGE):
    """A book of the catalog."""

    isbn: str = field(1, min_length=10, description="ISBN-10 or ISBN-13, without dashes.")
    title: str = field(2, min_length=1)
    author: str = field(3, default="")
    genre: Genre = field(4, default=Genre.UNSPECIFIED)
    price_cents: UInt32 = field(5, default=0)
    published: datetime.date | None = field(6, default=None)
    tags: list[str] = field(7, default_factory=list)
    stock: UInt32 = field(8, default=0)


class GetBookRequest(ProtoModel, package=PACKAGE):
    isbn: str = field(1)


class ListBooksRequest(ProtoModel, package=PACKAGE):
    genre: Genre | None = field(1, default=None, description="Only this genre, if set.")
    limit: UInt32 = field(2, default=0, description="At most this many books (0: all).")


class CreateBookRequest(ProtoModel, package=PACKAGE):
    book: Book = field(1)


class ImportSummary(ProtoModel, package=PACKAGE):
    imported: UInt32 = field(1, default=0)
    skipped: list[str] = field(2, default_factory=list, description="ISBNs already known.")


class StockChange(ProtoModel, package=PACKAGE):
    isbn: str = field(1)
    delta: Int32 = field(2, description="Books received (positive) or sold (negative).")


class StockLevel(ProtoModel, package=PACKAGE):
    isbn: str = field(1)
    stock: UInt32 = field(2, default=0)
