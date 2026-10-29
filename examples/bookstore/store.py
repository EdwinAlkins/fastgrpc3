"""An in-memory book store, standing for a database."""

import asyncio
import datetime
from dataclasses import dataclass, field

from bookstore.models import Book, Genre


class BookNotFound(LookupError):
    """No book with this ISBN."""

    def __init__(self, isbn: str) -> None:
        super().__init__(isbn)
        self.isbn = isbn


class OutOfStock(ValueError):
    """A sale larger than the stock."""

    def __init__(self, isbn: str, stock: int) -> None:
        super().__init__(f"{isbn}: only {stock} left")
        self.isbn = isbn
        self.stock = stock


@dataclass
class BookStore:
    books: dict[str, Book] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @classmethod
    def with_samples(cls) -> "BookStore":
        store = cls()
        for book in SAMPLES:
            store.books[book.isbn] = book
        return store

    def get(self, isbn: str) -> Book:
        try:
            return self.books[isbn]
        except KeyError:
            raise BookNotFound(isbn) from None

    def search(self, genre: Genre | None = None, limit: int = 0) -> list[Book]:
        books = [b for b in self.books.values() if genre is None or b.genre == genre]
        return books[:limit] if limit else books

    def add(self, book: Book) -> bool:
        """Add a book; False if its ISBN is already known."""
        if book.isbn in self.books:
            return False
        self.books[book.isbn] = book
        return True

    async def adjust_stock(self, isbn: str, delta: int) -> int:
        async with self.lock:
            book = self.get(isbn)
            if book.stock + delta < 0:
                raise OutOfStock(isbn, book.stock)
            self.books[isbn] = book.model_copy(update={"stock": book.stock + delta})
            return book.stock + delta


SAMPLES = [
    Book(
        isbn="9782070360024",
        title="L'Étranger",
        author="Albert Camus",
        genre=Genre.NOVEL,
        price_cents=750,
        published=datetime.date(1942, 5, 19),
        tags=["classic"],
        stock=4,
    ),
    Book(
        isbn="9782070323814",
        title="Le Mythe de Sisyphe",
        author="Albert Camus",
        genre=Genre.ESSAY,
        price_cents=890,
        published=datetime.date(1942, 10, 16),
        stock=2,
    ),
    Book(
        isbn="9782070321803",
        title="Alcools",
        author="Guillaume Apollinaire",
        genre=Genre.POETRY,
        price_cents=520,
        published=datetime.date(1913, 4, 20),
        tags=["classic", "poetry"],
        stock=1,
    ),
]
