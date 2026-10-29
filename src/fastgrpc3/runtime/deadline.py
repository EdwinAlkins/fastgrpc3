"""Deadline propagation: a call made from a handler inherits what is left of the request's.

The server stores the deadline of the incoming call in a `ContextVar` (local to the task, so
concurrent requests do not see each other's); the client caps its timeout with it.
"""

import contextvars
import time

__all__ = ["remaining", "reset_deadline", "set_deadline"]

_deadline: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "fastgrpc3_deadline", default=None
)


def set_deadline(seconds: float | None) -> contextvars.Token[float | None]:
    """Record the time left to the current call (`None`: no deadline)."""
    deadline = None if seconds is None else time.monotonic() + max(0.0, seconds)
    return _deadline.set(deadline)


def reset_deadline(token: contextvars.Token[float | None]) -> None:
    """Restore the deadline seen before `set_deadline`."""
    _deadline.reset(token)


def remaining() -> float | None:
    """Seconds left to the current call's deadline, or `None` if there is none."""
    deadline = _deadline.get()
    return None if deadline is None else max(0.0, deadline - time.monotonic())
