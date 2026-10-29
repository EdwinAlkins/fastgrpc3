# Getting started

This page builds a small notes service, code first: the contract, the server, a client and
a test. Each step is a file you can run.

## 1. The contract

Messages are Pydantic models. `ProtoModel` adds what a protobuf contract needs, and
`field(n)` gives each field its number on the wire; any `pydantic.Field` argument still
works (`min_length`, `description`…).

```python title="notes/models.py"
import datetime

from fastgrpc3 import ProtoModel, field


class Note(ProtoModel):
    id: str = field(1)
    text: str = field(2, min_length=1)
    created_at: datetime.datetime | None = field(3, default=None)
    tags: list[str] = field(4, default_factory=list)


class GetNoteRequest(ProtoModel):
    id: str = field(1)


class SearchRequest(ProtoModel):
    tag: str = field(1)
```

`datetime` becomes a `google.protobuf.Timestamp`, `list[str]` a `repeated string`: see
[Type mapping](types.md). Without `field(n)`, numbers come from `proto.lock` (see
[Contracts](contracts.md#field-numbers)).

## 2. The service

A `GrpcService` has a name and a package; each RPC is an `async` function under a
decorator. The RPC name is the function's name (or `@service.unary("Name")`).

```python title="notes/service.py"
from collections.abc import AsyncIterator

from fastgrpc3 import Depends, GrpcError, GrpcService, StatusCode

from notes.models import GetNoteRequest, Note, SearchRequest

service = GrpcService("Notes", package="notes.v1")

NOTES = {"1": Note(id="1", text="Buy milk", tags=["home"])}


def get_notes() -> dict[str, Note]:
    return NOTES


@service.unary()
async def GetNote(req: GetNoteRequest, notes: dict[str, Note] = Depends(get_notes)) -> Note:
    if req.id not in notes:
        raise GrpcError(StatusCode.NOT_FOUND, f"no note {req.id}")
    return notes[req.id]


@service.unary_stream()
async def FindNotes(
    req: SearchRequest, notes: dict[str, Note] = Depends(get_notes)
) -> AsyncIterator[Note]:
    for note in notes.values():
        if req.tag in note.tags:
            yield note
```

Name the messages differently from the RPCs (`GetNoteRequest`, not `GetNote`): the
handler `GetNote` would otherwise hide the model of the same name.

The signature says everything: the parameter annotated with a model is the request, the
return annotation is the response (`AsyncIterator[Model]` for a stream), and
`Depends(...)` parameters are injected. A wrong signature fails when the module is
imported, not at the first call.

## 3. The server

```python title="notes/app.py"
import asyncio

from fastgrpc3 import GrpcApp, Insecure

from notes.service import service

app = GrpcApp(address="[::]:50051", tls=Insecure())
app.add_service(service)
app.enable_health()
app.enable_reflection()

if __name__ == "__main__":
    asyncio.run(app.run())
```

The transport is always explicit: `Insecure()` here, `TLS(cert=..., key=...)` in
production. Run it with `python -m notes.app`, or `fastgrpc3 run notes.app:app`.

When it starts, the app renders the `.proto` of its services, compiles it with protoruf
and binds a Rust codec to each model. `fastgrpc3 export notes.app:app -o proto/` writes
those files, for clients in other languages.

## 4. A client

A client class lists the RPCs with the same models:

```python title="notes/client.py"
import asyncio

from fastgrpc3 import Channel, GrpcClient, GrpcError, Insecure

from notes.models import GetNoteRequest, Note, SearchRequest


class NotesClient(GrpcClient):
    package = "notes.v1"
    service = "Notes"
    get_note = GrpcClient.unary("GetNote", GetNoteRequest, Note)
    find_notes = GrpcClient.unary_stream("FindNotes", SearchRequest, Note)


async def main() -> None:
    async with Channel("localhost:50051", tls=Insecure()) as channel:
        notes = NotesClient(channel)
        print(await notes.get_note(GetNoteRequest(id="1")))
        async for note in notes.find_notes(SearchRequest(tag="home")):
            print(note.text)
        try:
            await notes.get_note(GetNoteRequest(id="42"))
        except GrpcError as error:
            print(error.code.name, error.message)  # NOT_FOUND no note 42


if __name__ == "__main__":
    asyncio.run(main())
```

A unary call is awaited; a server stream is iterated. Every method is typed: mypy knows
that `get_note` takes a `GetNoteRequest` and returns a `Note`.

## 5. A test

`TestClient` starts the app on a free local port and opens a channel to it:

```python title="tests/test_notes.py"
import pytest
from fastgrpc3 import GrpcError, StatusCode
from fastgrpc3.testing import TestClient

from notes.app import app
from notes.client import NotesClient
from notes.models import GetNoteRequest
from notes.service import get_notes


async def test_unknown_note() -> None:
    app.dependency_overrides[get_notes] = lambda: {}  # an empty store for this test
    async with TestClient(app) as tc:
        with pytest.raises(GrpcError) as raised:
            await tc.client(NotesClient).get_note(GetNoteRequest(id="1"))
    assert raised.value.code == StatusCode.NOT_FOUND
```

(With `pytest-asyncio` in `asyncio_mode = "auto"`.) See [Testing](testing.md).

## Next

- [Contracts](contracts.md): field numbers and `proto.lock`, enums, `oneof`, packages.
- [Server](server.md): `Context`, dependencies, hooks, middlewares, errors, health, TLS.
- [Client](client.md): streams, metadata, timeouts, retries, TLS and tokens.
- [Proto first](proto-first.md): start from `.proto` files instead.
