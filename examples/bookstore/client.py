"""A client of the bookstore, calling every RPC.

uv run python -m bookstore.client [--target localhost:50051]
"""

import argparse
import asyncio
import datetime
from collections.abc import AsyncIterator

from fastgrpc3 import Channel, ErrorInfo, GrpcClient, GrpcError, Insecure

from bookstore.models import (
    PACKAGE,
    Book,
    CreateBookRequest,
    Genre,
    GetBookRequest,
    ImportSummary,
    ListBooksRequest,
    StockChange,
    StockLevel,
)


class BookstoreClient(GrpcClient):
    """Typed client: each attribute is one RPC of `bookstore.v1.Bookstore`."""

    package = PACKAGE
    service = "Bookstore"

    get_book = GrpcClient.unary("GetBook", GetBookRequest, Book)
    list_books = GrpcClient.unary_stream("ListBooks", ListBooksRequest, Book)
    create_book = GrpcClient.unary("CreateBook", CreateBookRequest, Book)
    import_books = GrpcClient.stream_unary("ImportBooks", Book, ImportSummary)
    adjust_stock = GrpcClient.stream_stream("AdjustStock", StockChange, StockLevel)


async def demo(client: BookstoreClient) -> None:
    # Unary.
    book = await client.get_book(GetBookRequest(isbn="9782070360024"))
    print(f"GetBook: {book.title} by {book.author}, {book.price_cents / 100:.2f} €")

    # Errors come back as GrpcError, with their google.rpc details.
    try:
        await client.get_book(GetBookRequest(isbn="0000000000"))
    except GrpcError as error:
        reason = info.reason if (info := error.detail(ErrorInfo)) else None
        print(f"GetBook unknown: {error.code.name} {error.message!r}, reason {reason}")

    # Server stream: iterate, do not await.
    essays = [b.title async for b in client.list_books(ListBooksRequest(genre=Genre.ESSAY))]
    print(f"ListBooks(ESSAY): {essays}")

    # Metadata goes with the call; the server checks x-user for CreateBook.
    new_book = Book(
        isbn="9782070368228",
        title="La Peste",
        author="Albert Camus",
        genre=Genre.NOVEL,
        price_cents=850,
        published=datetime.date(1947, 6, 10),
    )
    try:
        await client.create_book(CreateBookRequest(book=new_book))
    except GrpcError as error:
        print(f"CreateBook as anonymous: {error.code.name}")
    created = await client.create_book(
        CreateBookRequest(book=new_book), metadata={"x-user": "admin"}, timeout=5.0
    )
    print(f"CreateBook as admin: {created.title}")

    # Client stream: pass an iterable (or an async iterator) of requests.
    summary = await client.import_books(
        [
            new_book,  # already there now: skipped
            Book(isbn="9782070411443", title="Calligrammes", author="Guillaume Apollinaire"),
        ]
    )
    print(f"ImportBooks: {summary.imported} imported, skipped {summary.skipped}")

    # Bidirectional stream: requests produced as we go, a response for each.
    async def changes() -> AsyncIterator[StockChange]:
        yield StockChange(isbn="9782070360024", delta=+3)
        yield StockChange(isbn="9782070360024", delta=-2)
        yield StockChange(isbn="9782070321803", delta=-5)  # more than in stock

    try:
        async for level in client.adjust_stock(changes()):
            print(f"AdjustStock: {level.isbn} -> {level.stock}")
    except GrpcError as error:
        print(f"AdjustStock stopped: {error.code.name} {error.message!r}")


async def main(target: str) -> None:
    async with Channel(target, tls=Insecure(), default_timeout=10.0) as channel:
        await demo(BookstoreClient(channel))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--target", default="localhost:50051")
    asyncio.run(main(parser.parse_args().target))
