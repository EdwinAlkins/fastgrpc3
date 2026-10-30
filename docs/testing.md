# Testing

`fastgrpc3.testing.TestClient` starts an app on `127.0.0.1` and a free port, and opens a
channel to it. It is a real server, so tests go through the same encoding, validation,
hooks and errors as production.

```python
import pytest
from fastgrpc3 import GrpcError, StatusCode
from fastgrpc3.testing import TestClient

from myapp.main import app
from myapp.client import OrdersClient
from myapp.deps import get_repository


@pytest.fixture
async def orders():
    app.dependency_overrides[get_repository] = lambda: FakeRepository()
    try:
        async with TestClient(app) as tc:
            yield tc.client(OrdersClient)
    finally:
        app.dependency_overrides.clear()


async def test_unknown_order(orders: OrdersClient) -> None:
    with pytest.raises(GrpcError) as raised:
        await orders.get_order(GetOrderRequest(order_number="?"))
    assert raised.value.code == StatusCode.NOT_FOUND
```

- The app must use `tls=Insecure()`; `TestClient` replaces its address.
- `tc.channel` is the `Channel`, for calls without a client class (the raw `grpc.aio`
  channel is `tc.channel.grpc_channel`).
- `TestClient(app, default_timeout=10.0)` sets the channel's timeout.
- Overrides are read when the app starts: set them before `async with TestClient(app)`.

With pytest, use `pytest-asyncio` in auto mode:

```toml title="pyproject.toml"
[tool.pytest.ini_options]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"
```

Handlers stay plain functions: the decorators return them unchanged, so a unit test can
also call one directly with a request and fake dependencies.

[`examples/tests/test_bookstore.py`](https://github.com/EdwinAlkins/fastgrpc3/blob/main/examples/tests/test_bookstore.py) tests every RPC of the example service this way.
