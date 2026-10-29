"""The bookstore through a real local server: `TestClient` starts the app on a free port."""

from collections.abc import AsyncIterator

import pytest
from fastgrpc3 import ErrorInfo, GrpcError, StatusCode
from fastgrpc3.testing import TestClient

from bookstore.app import app
from bookstore.client import BookstoreClient
from bookstore.models import (
    Book,
    CreateBookRequest,
    Genre,
    GetBookRequest,
    ListBooksRequest,
    StockChange,
)
from bookstore.service import get_store
from bookstore.store import BookStore


@pytest.fixture
async def client() -> AsyncIterator[BookstoreClient]:
    """A client of the app, with a fresh store for each test."""
    store = BookStore.with_samples()
    app.dependency_overrides[get_store] = lambda: store
    try:
        async with TestClient(app) as tc:
            yield tc.client(BookstoreClient)
    finally:
        app.dependency_overrides.clear()


async def test_get_book(client: BookstoreClient) -> None:
    book = await client.get_book(GetBookRequest(isbn="9782070360024"))
    assert (book.title, book.genre) == ("L'Étranger", Genre.NOVEL)


async def test_unknown_book_is_not_found_with_details(client: BookstoreClient) -> None:
    with pytest.raises(GrpcError) as raised:
        await client.get_book(GetBookRequest(isbn="0000000000"))
    assert raised.value.code == StatusCode.NOT_FOUND
    assert raised.value.detail(ErrorInfo) == ErrorInfo(
        reason="BOOK_NOT_FOUND", domain="bookstore.example", metadata={"isbn": "0000000000"}
    )


async def test_list_books_streams_the_catalog(client: BookstoreClient) -> None:
    every = [b.isbn async for b in client.list_books(ListBooksRequest())]
    poetry = [b.title async for b in client.list_books(ListBooksRequest(genre=Genre.POETRY))]
    assert len(every) == 3
    assert poetry == ["Alcools"]
    assert len([b async for b in client.list_books(ListBooksRequest(limit=2))]) == 2


async def test_create_book_needs_an_admin(client: BookstoreClient) -> None:
    request = CreateBookRequest(book=Book(isbn="9782070368228", title="La Peste"))
    with pytest.raises(GrpcError) as raised:
        await client.create_book(request, metadata={"x-user": "bob"})
    assert raised.value.code == StatusCode.PERMISSION_DENIED

    created = await client.create_book(request, metadata={"x-user": "admin"})
    assert created.title == "La Peste"
    with pytest.raises(GrpcError) as raised:
        await client.create_book(request, metadata={"x-user": "admin"})
    assert raised.value.code == StatusCode.ALREADY_EXISTS


async def test_invalid_books_are_refused_by_the_server(client: BookstoreClient) -> None:
    # model_construct skips the client-side validation, to show the server's.
    invalid = Book.model_construct(isbn="123", title="")
    with pytest.raises(GrpcError) as raised:
        await client.create_book(
            CreateBookRequest.model_construct(book=invalid), metadata={"x-user": "admin"}
        )
    assert raised.value.code == StatusCode.INVALID_ARGUMENT


async def test_import_books_skips_known_ones(client: BookstoreClient) -> None:
    summary = await client.import_books(
        [Book(isbn="9782070360024", title="L'Étranger"), Book(isbn="9782070411443", title="X")]
    )
    assert (summary.imported, summary.skipped) == (1, ["9782070360024"])


async def test_adjust_stock_answers_each_change(client: BookstoreClient) -> None:
    changes = [StockChange(isbn="9782070360024", delta=d) for d in (+1, -3)]
    levels = [level.stock async for level in client.adjust_stock(changes)]
    assert levels == [5, 2]

    with pytest.raises(GrpcError) as raised:
        async for _ in client.adjust_stock([StockChange(isbn="9782070321803", delta=-5)]):
            pass
    assert raised.value.code == StatusCode.FAILED_PRECONDITION
