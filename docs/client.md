# Client

## Channel

A `Channel` is one connection, shared by any number of clients:

```python
from fastgrpc3 import TLS, BearerToken, Channel

async with Channel(
    "orders.example.test:443",
    tls=TLS(ca="ca.pem"),  # TLS() alone trusts the system roots
    auth=BearerToken("sk_example_not_a_real_secret"),
    default_timeout=5.0,
) as channel:
    ...
```

| Argument | Default | |
|----------|---------|-|
| `tls` | — (required) | `TLS(ca=...)`, `MTLS(cert, key, ca)` or `Insecure()` |
| `auth` | `None` | `BearerToken(...)`: `authorization: Bearer …` on every call; refused on an insecure channel |
| `default_timeout` | 30 s | per-call timeout when the call gives none; `None` for no timeout |
| `retry` | `None` | `RetryPolicy(methods=("orders.v1.Orders/GetOrder",), max_attempts=3)`, idempotent methods only |
| `keepalive` | `None` | `Keepalive(...)` |
| `compression`, `max_send_bytes`, `max_recv_bytes`, `max_metadata_bytes` | | as on the server |

Over an insecure channel, credentials are refused: an `auth`, and metadata keys such as
`authorization`, `x-api-key` or `cookie` raise `ValueError` instead of being sent in clear.

## Typed clients

```python
from fastgrpc3 import GrpcClient


class OrdersClient(GrpcClient):
    package = "orders.v1"
    service = "Orders"

    get_order = GrpcClient.unary("GetOrder", GetOrderRequest, Order)
    watch = GrpcClient.unary_stream("Watch", GetOrderRequest, Order)
    upload = GrpcClient.stream_unary("Upload", Line, Summary)
    chat = GrpcClient.stream_stream("Chat", Message, Message)
```

In proto-first mode, `fastgrpc3 generate` writes this class for you (`OrdersClient` in
`orders_service.py`). The models are the server's, or copies with the same field numbers.
If the models leave their numbers to `proto.lock`, set `lock_file = "proto.lock"` on the
class, pointing at the server's lock.

```python
orders = OrdersClient(channel)

order = await orders.get_order(GetOrderRequest(order_number="A"))  # await a unary call

async for update in orders.watch(GetOrderRequest(order_number="A")):  # iterate a stream
    ...

summary = await orders.upload([Line(sku="a"), Line(sku="b")])  # an iterable or async iterator


async def messages() -> AsyncIterator[Message]:
    yield Message(text="hi")


async for reply in orders.chat(messages()):
    ...
```

Every method takes `timeout=` (seconds) and `metadata=` (a `Metadata`, a `dict` or pairs).
Methods are typed: mypy checks the request type and knows the response type.

## Errors

A failed call raises `GrpcError`, with the status code, the message and the error details:

```python
from fastgrpc3 import ErrorInfo, GrpcError, StatusCode

try:
    await orders.get_order(GetOrderRequest(order_number="?"))
except GrpcError as error:
    if error.code == StatusCode.NOT_FOUND:
        info = error.detail(ErrorInfo)  # the first ErrorInfo detail, or None
```

The standard `google.rpc` details are decoded; a detail of your own needs
`@register_detail` on its model. An unknown detail stays an `AnyMessage` (its `type_url`
and bytes).

The client decodes responses itself: a response that does not decode or validate raises
`GrpcError(INTERNAL, "invalid response payload")`, instead of silently giving `None`.

## Deadlines

A call made while serving a request inherits what is left of that request's deadline: the
timeout is the smallest of the call's `timeout` (or the channel's `default_timeout`) and the
time left. A chain of services therefore shares one deadline, with nothing to pass around.
