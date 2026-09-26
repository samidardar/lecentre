from __future__ import annotations

import asyncio
import random
import time
from typing import Awaitable, Callable, TypeVar

T = TypeVar("T")


class CircuitOpenError(RuntimeError):
    pass


class CircuitBreaker:
    """Ouvre le circuit après `threshold` échecs consécutifs, retente après `reset_after` secondes."""

    def __init__(self, name: str, threshold: int = 5, reset_after: float = 30.0) -> None:
        self.name = name
        self.threshold = threshold
        self.reset_after = reset_after
        self._failures = 0
        self._opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        if time.monotonic() - self._opened_at >= self.reset_after:
            return False  # half-open : on laisse passer un essai
        return True

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.threshold:
            self._opened_at = time.monotonic()

    async def call(self, fn: Callable[[], Awaitable[T]]) -> T:
        if self.is_open:
            raise CircuitOpenError(self.name)
        try:
            result = await fn()
        except Exception:
            self.record_failure()
            raise
        self.record_success()
        return result


async def retry_async(
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay: float = 0.2,
    max_delay: float = 5.0,
    timeout: float | None = None,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
) -> T:
    last: BaseException | None = None
    for i in range(attempts):
        try:
            coro = fn()
            return await (asyncio.wait_for(coro, timeout) if timeout else coro)
        except retry_on as exc:  # noqa: PERF203
            last = exc
            if i == attempts - 1:
                break
            delay = min(max_delay, base_delay * (2**i)) * (0.5 + random.random() / 2)
            await asyncio.sleep(delay)
    assert last is not None
    raise last
