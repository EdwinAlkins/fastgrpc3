"""The `bookstore.v1.Bookstore` service: one handler per RPC, the four call kinds.

| RPC | Kind | Shows |
|-----|------|-------|
| GetBook | unary | a dependency, a business exception mapped to NOT_FOUND |
| ListBooks | server stream | an async generator handler |
| CreateBook | unary | sub-dependencies (the caller must be an admin), ALREADY_EXISTS |
| ImportBooks | client stream | reading an async iterator of requests |
| AdjustStock | bidirectional | a response per request, an error in the middle of a stream |
"""

from collections.abc import AsyncIterator

from fastgrpc3 import Context, Depends, ErrorInfo, GrpcError, GrpcService, StatusCode

from bookstore.models import (
    PACKAGE,
    Book,
    CreateBookRequest,
    GetBookRequest,
    ImportSummary,
    ListBooksRequest,
    StockChange,
    StockLevel,
)
from bookstore.store import BookStore

service = GrpcService("Bookstore", package=PACKAGE, doc="A catalog of books and their stock.")

STORE = BookStore.with_samples()


def get_store() -> BookStore:
    """The store; tests replace it through `app.dependency_overrides`."""
    return STORE


def current_user(ctx: Context) -> str:
    """Who calls, from the `x-user` header (an identity proxy would set it)."""
    return str(ctx.metadata.get("x-user", "anonymous"))


def require_admin(user: str = Depends(current_user)) -> str:
    if user != "admin":
        raise GrpcError(StatusCode.PERMISSION_DENIED, f"{user} may not add books")
    return user


@service.unary()
async def GetBook(req: GetBookRequest, store: BookStore = Depends(get_store)) -> Book:
    """One book by ISBN."""
    return store.get(req.isbn)  # BookNotFound becomes NOT_FOUND (see app.py)


@service.unary_stream()
async def ListBooks(
    req: ListBooksRequest, store: BookStore = Depends(get_store)
) -> AsyncIterator[Book]:
    """The catalog, one book per message."""
    for book in store.search(req.genre, req.limit):
        yield book


@service.unary()
async def CreateBook(
    req: CreateBookRequest,
    admin: str = Depends(require_admin),
    store: BookStore = Depends(get_store),
) -> Book:
    """Add a book (admins only)."""
    if not store.add(req.book):
        info = ErrorInfo(
            reason="BOOK_EXISTS", domain="bookstore.example", metadata={"isbn": req.book.isbn}
        )
        raise GrpcError(StatusCode.ALREADY_EXISTS, f"book {req.book.isbn} already exists", [info])
    return req.book


@service.stream_unary()
async def ImportBooks(
    books: AsyncIterator[Book], store: BookStore = Depends(get_store)
) -> ImportSummary:
    """Add many books in one call; the known ones are skipped."""
    summary = ImportSummary()
    async for book in books:
        if store.add(book):
            summary.imported += 1
        else:
            summary.skipped.append(book.isbn)
    return summary


@service.stream_stream()
async def AdjustStock(
    changes: AsyncIterator[StockChange], store: BookStore = Depends(get_store)
) -> AsyncIterator[StockLevel]:
    """Apply stock changes as they come, and answer each with the new level."""
    async for change in changes:
        stock = await store.adjust_stock(change.isbn, change.delta)  # OutOfStock: see app.py
        yield StockLevel(isbn=change.isbn, stock=stock)
