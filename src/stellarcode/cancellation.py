from __future__ import annotations

import threading
from collections.abc import Callable
from contextvars import copy_context
from typing import TypeVar


T = TypeVar("T")


class TaskCancelledError(RuntimeError):
    """Raised when an active Runtime task is cancelled by the user."""


def raise_if_cancelled(cancellation_event: threading.Event | None) -> None:
    if cancellation_event is not None and cancellation_event.is_set():
        raise TaskCancelledError("Task cancelled by user.")


def cancellable_call(
    call: Callable[[], T],
    cancellation_event: threading.Event | None,
    *,
    poll_interval: float = 0.05,
) -> T:
    """Run a blocking call while allowing the Runtime task to stop promptly.

    Python's synchronous HTTP clients cannot interrupt an in-flight request. The call
    therefore runs in a daemon thread when cancellation is enabled; cancellation stops
    waiting immediately and ignores the eventual result.
    """

    if cancellation_event is None:
        return call()
    raise_if_cancelled(cancellation_event)

    completed = threading.Event()
    result: list[T] = []
    error: list[BaseException] = []

    def invoke() -> None:
        try:
            result.append(call())
        except BaseException as exc:
            error.append(exc)
        finally:
            completed.set()

    context = copy_context()
    threading.Thread(
        target=lambda: context.run(invoke),
        name="stellarcode-cancellable-call",
        daemon=True,
    ).start()
    while not completed.wait(poll_interval):
        raise_if_cancelled(cancellation_event)
    raise_if_cancelled(cancellation_event)
    if error:
        raise error[0]
    return result[0]
