"""
Framework-agnostic single-flight coordinators.

These classes implement the "double-checked locking" algorithm that ensures
a refresh function is called **exactly once** even when many threads or
coroutines race to handle concurrent 401 responses simultaneously.

Algorithm (same for both sync and async variants):
    1. A request receives 401 and records the stale token T_stale.
    2. Acquire the lock (threads block; coroutines yield without freezing the event loop).
    3. **After** acquiring the lock, re-read the current token T_current.
    4a. If T_current != T_stale  → another waiter already refreshed; return T_current.
    4b. If T_current == T_stale  → we are the winner; call refresh() and return result.
    5. Release lock (automatic via context manager).

This means N concurrent requests racing on the same stale token produce
exactly 1 refresh call; the remaining N-1 get the new token for free.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from typing import Generic, TypeVar

T = TypeVar("T")


class SyncCoordinator(Generic[T]):
    """Single-flight coordinator for synchronous (threaded) code.

    Uses :class:`threading.Lock` so callers block until the in-flight
    refresh completes.  Safe to share across threads.

    Args:
        get_token: A zero-argument callable that returns the current token.
                   Must be cheap (e.g. dict lookup), never performs I/O.
        refresh:   A zero-argument callable that fetches a fresh token,
                   persists it (so ``get_token`` will return it next call),
                   and returns the new token value.

    Example::

        coordinator = SyncCoordinator(
            get_token=lambda: store["access_token"],
            refresh=my_refresh_fn,
        )
        new_token = coordinator.resolve(stale_token=old_token)
    """

    def __init__(
        self,
        get_token: Callable[[], T],
        refresh: Callable[[], T],
    ) -> None:
        self._get_token = get_token
        self._refresh = refresh
        self._lock = threading.Lock()

    def resolve(self, stale_token: T) -> T:
        """Return a valid token, calling ``refresh()`` at most once per stale value.

        Args:
            stale_token: The token that was rejected (caused the 401).

        Returns:
            A fresh token — either fetched by this call or already obtained
            by another thread that won the race.
        """
        with self._lock:
            # Double-check: another thread may have already refreshed while
            # we were waiting for the lock.
            current = self._get_token()
            if current != stale_token:
                # Someone else refreshed; use their result.
                return current
            # We are the winner — perform the refresh.
            return self._refresh()


class AsyncCoordinator(Generic[T]):
    """Single-flight coordinator for asynchronous (asyncio) code.

    Uses :class:`asyncio.Lock` so coroutines *yield* while waiting,
    keeping the event loop responsive.  Never uses :class:`threading.Lock`,
    which would freeze the entire event loop.

    Args:
        get_token: A zero-argument callable (sync) that returns the current
                   token.  Must be cheap — no I/O, no ``await``.
        refresh:   An async zero-argument callable that fetches a fresh token,
                   persists it, and returns the new token value.

    Example::

        coordinator = AsyncCoordinator(
            get_token=lambda: store["access_token"],
            refresh=my_async_refresh_fn,
        )
        new_token = await coordinator.resolve(stale_token=old_token)
    """

    def __init__(
        self,
        get_token: Callable[[], T],
        refresh: Callable[[], Awaitable[T]],
    ) -> None:
        self._get_token = get_token
        self._refresh = refresh
        self._lock = asyncio.Lock()

    async def resolve(self, stale_token: T) -> T:
        """Return a valid token, calling ``refresh()`` at most once per stale value.

        Args:
            stale_token: The token that was rejected (caused the 401).

        Returns:
            A fresh token — either fetched by this coroutine or already obtained
            by another coroutine that won the race.
        """
        async with self._lock:
            # Double-check: another coroutine may have refreshed while we
            # were suspended waiting for the lock.
            current = self._get_token()
            if current != stale_token:
                # Someone else refreshed; use their result.
                return current
            # We are the winner — perform the async refresh.
            return await self._refresh()
