"""
Framework-agnostic single-flight coordinators.

These classes implement the "double-checked locking" algorithm that ensures
a refresh function is called **exactly once** even when many threads or
coroutines race to handle concurrent 401 responses simultaneously.

.. _singleflight-core:

Algorithm (same for both sync and async variants):
    1. Before sending, a request records the coordinator's *generation* G and
       the token value T it is about to send.
    2. The request receives 401 and calls ``resolve(seen_generation=G, stale_token=T)``.
    3. Acquire the lock (threads block; coroutines yield without freezing the event loop).
    4. **After** acquiring the lock, re-check, in this order:
    4a. generation != G      → another waiter already refreshed; return ``get_token()``.
    4b. ``get_token() != T`` → somebody *outside* this coordinator refreshed; use theirs.
    4c. otherwise            → we are the winner; call ``refresh()`` and bump the generation.
    5. Release lock (automatic via context manager).

This means N concurrent requests racing on the same generation produce
exactly 1 refresh call; the remaining N-1 get the new token for free.

**Why the generation counter comes first.**  Comparing token *values* alone
silently degrades to zero protection whenever ``refresh()`` returns the same
string it returned before — idempotent/cached refresh endpoints, servers with
token rotation disabled, fixed staging tokens, or 401s that were never about
expiry in the first place.  In those cases every waiter sees
``current == stale``, so every waiter refreshes.  A monotonic counter owned by
the coordinator does not depend on ``get_token``/``refresh`` behaviour at all.

**Why the token check is still kept (4b).**  The generation only moves when
*this* coordinator refreshes.  Plenty of deployments renew the token some
other way — a background TTL thread, a sidecar, another process writing the
same token store.  Dropping the value comparison would make those setups
perform a redundant refresh on every 401.  Checking the generation first and
the value second keeps both properties: value-independence for the
single-flight guarantee, and value-awareness for externally renewed tokens.

**Reentrancy protection:** ``threading.Lock`` and ``asyncio.Lock`` are *not*
reentrant.  If ``refresh()`` triggers the auth flow on the same client
(causing ``resolve()`` to be called again from the same thread/task), the
process would deadlock silently.  Instead, the coordinators track the
current holder and raise :class:`ReentrantRefreshError` immediately.  The
holder flag lives in :class:`threading.local` / a :class:`~contextvars.ContextVar`
rather than a shared attribute, so it is never read outside the lock — which
also makes it correct on free-threaded (no-GIL) builds.

**Failure cooldown:** when ``refresh()`` raises, the generation does *not*
advance, so without extra care every remaining waiter would take the lock,
see the unchanged generation, and call the already-failing refresh endpoint
again — an N-fold thundering herd aimed at an auth server that is by
definition already unhealthy.  The failure is therefore cached for
``error_cooldown`` seconds and replayed to waiters in that window.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Generic, TypeVar
from weakref import WeakKeyDictionary

from ._exceptions import ReentrantRefreshError, RefreshTimeoutError

T = TypeVar("T")

#: Default window during which a failed ``refresh()`` is replayed to waiters
#: instead of being retried.  Chosen to outlive a burst of concurrent 401s
#: without noticeably delaying recovery.
DEFAULT_ERROR_COOLDOWN = 1.0

_REENTRANCY_HINT = (
    "Use a separate, unauthenticated client inside refresh(). "
    "See: https://github.com/alibeg-begow/singleflight_auth#refresh-must-use-a-separate-client"
)


def _replay(exc: Exception) -> Exception:
    """Return a fresh copy of *exc* to hand to a waiter during the cooldown.

    Re-raising the cached instance itself would append a new frame to its
    ``__traceback__`` on every replay, growing without bound for as long as
    the auth server stays down.
    """
    try:
        clone = type(exc)(*exc.args)
    except Exception:  # pragma: no cover — exotic exception signatures
        return exc
    clone.__cause__ = exc.__cause__
    return clone


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
        error_cooldown: Seconds during which a failed ``refresh()`` is replayed
                   to waiters instead of being retried.  ``0`` disables it.
        timeout:   Maximum seconds to wait **for the lock** — i.e. for another
                   caller's in-flight refresh — before raising
                   :class:`RefreshTimeoutError`.  ``None`` waits forever.
                   It does **not** abort the winner's own ``refresh()`` call:
                   a blocking callable cannot be interrupted, so give
                   ``refresh()`` its own I/O timeout as well.

    Example::

        coordinator = SyncCoordinator(
            get_token=lambda: store["access_token"],
            refresh=my_refresh_fn,
        )
        generation = coordinator.token_generation()
        token = store["access_token"]
        # ... request with `token` gets a 401 ...
        new_token, generation = coordinator.resolve(generation, stale_token=token)
    """

    def __init__(
        self,
        get_token: Callable[[], T],
        refresh: Callable[[], T],
        *,
        error_cooldown: float = DEFAULT_ERROR_COOLDOWN,
        timeout: float | None = None,
    ) -> None:
        self._get_token = get_token
        self._refresh = refresh
        self._error_cooldown = error_cooldown
        self._timeout = timeout
        self._lock = threading.Lock()
        self._local = threading.local()
        self._generation = 0
        self._last_error: tuple[float, Exception] | None = None

    def token_generation(self) -> int:
        """Return the current generation; pass it back to :meth:`resolve` on 401.

        Read without the lock on purpose.  The counter only ever moves
        forward, so a stale read is safe: ``resolve`` re-reads it under the
        lock and correctly concludes that somebody else already refreshed.
        """
        return self._generation

    def resolve(self, seen_generation: int, stale_token: T) -> tuple[T, int]:
        """Return a valid token, calling ``refresh()`` at most once per generation.

        Args:
            seen_generation: The generation observed when the rejected request
                was sent (from :meth:`token_generation`).
            stale_token: The token value that was actually rejected. Used only
                as a secondary check for refreshes performed *outside* this
                coordinator — see :ref:`the module docstring <singleflight-core>`.

        Returns:
            ``(token, generation)`` — a fresh token, either fetched by this
            call or already obtained by the thread that won the race, plus the
            generation to carry into the next attempt.

        Raises:
            ReentrantRefreshError: If the current thread already holds the
                lock (i.e. ``refresh()`` triggered a nested auth flow on
                the same client).
            RefreshTimeoutError: If ``timeout`` elapsed while *waiting* for
                another caller's in-flight refresh. The winner's own
                ``refresh()`` is never interrupted.
        """
        if getattr(self._local, "holding", False):
            raise ReentrantRefreshError(
                "refresh() triggered the auth flow on the same client/session, "
                "causing a reentrant lock acquisition on the same thread. "
                + _REENTRANCY_HINT
            )

        if self._timeout is None:
            self._lock.acquire()
        elif not self._lock.acquire(timeout=self._timeout):
            raise RefreshTimeoutError(
                f"Timed out after {self._timeout}s waiting for an in-flight token refresh"
            )

        self._local.holding = True
        try:
            # Double-check: another thread may have already refreshed while
            # we were waiting for the lock.
            if self._generation != seen_generation:
                return self._get_token(), self._generation

            # Secondary check for refreshes we did not perform: a background
            # TTL thread or another process writing to the shared token store
            # leaves the generation untouched but the token value changed.
            current = self._get_token()
            if current != stale_token:
                return current, self._generation

            cached = self._last_error
            if cached is not None and time.monotonic() - cached[0] < self._error_cooldown:
                # The auth server just failed us; do not pile on.
                raise _replay(cached[1])

            # We are the winner — perform the refresh.
            try:
                token = self._refresh()
            except Exception as exc:
                self._last_error = (time.monotonic(), exc)
                raise
            self._last_error = None
            self._generation += 1
            return token, self._generation
        finally:
            self._local.holding = False
            self._lock.release()


class AsyncCoordinator(Generic[T]):
    """Single-flight coordinator for asynchronous (asyncio) code.

    Uses :class:`asyncio.Lock` so coroutines *yield* while waiting,
    keeping the event loop responsive.  Never uses :class:`threading.Lock`
    for the wait, which would freeze the entire event loop.

    A separate lock is kept **per event loop**.  ``asyncio.Lock`` binds itself
    to the running loop the first time it is contended and raises
    ``RuntimeError: ... is bound to a different event loop`` afterwards on any
    other loop.  Since the documented usage is to create one auth instance and
    share it process-wide, that instance routinely outlives a single loop:
    ``pytest-asyncio`` builds a loop per test, notebooks and CLI batch jobs
    call :func:`asyncio.run` repeatedly, and thread-per-loop servers create one
    each.  Keying the lock by loop keeps a shared instance usable in all of
    them.  The locks are held weakly so finished loops are not kept alive.

    Args:
        get_token: A zero-argument callable (sync) that returns the current
                   token.  Must be cheap — no I/O, no ``await``.
        refresh:   An async zero-argument callable that fetches a fresh token,
                   persists it, and returns the new token value.
        error_cooldown: Seconds during which a failed ``refresh()`` is replayed
                   to waiters instead of being retried.  ``0`` disables it.
        timeout:   Maximum seconds to wait **for the lock** — i.e. for another
                   caller's in-flight refresh — before raising
                   :class:`RefreshTimeoutError`.  ``None`` waits forever.
                   As in the sync coordinator it does **not** abort the
                   winner's own ``refresh()``; only the waiting is bounded.

    Example::

        coordinator = AsyncCoordinator(
            get_token=lambda: store["access_token"],
            refresh=my_async_refresh_fn,
        )
        generation = coordinator.token_generation()
        token = store["access_token"]
        # ... request with `token` gets a 401 ...
        new_token, generation = await coordinator.resolve(generation, stale_token=token)
    """

    def __init__(
        self,
        get_token: Callable[[], T],
        refresh: Callable[[], Awaitable[T]],
        *,
        error_cooldown: float = DEFAULT_ERROR_COOLDOWN,
        timeout: float | None = None,
    ) -> None:
        self._get_token = get_token
        self._refresh = refresh
        self._error_cooldown = error_cooldown
        self._timeout = timeout
        # Guards the lock registry and the shared generation/error state, all
        # of which may be touched from several event loops (and therefore
        # several threads) at once.  Never held across an await.
        self._guard = threading.Lock()
        self._locks: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = (
            WeakKeyDictionary()
        )
        self._holding: ContextVar[bool] = ContextVar("singleflight_holding", default=False)
        self._generation = 0
        self._last_error: tuple[float, Exception] | None = None

    def token_generation(self) -> int:
        """Return the current generation; pass it back to :meth:`resolve` on 401."""
        return self._generation

    def _lock_for_running_loop(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        with self._guard:
            lock = self._locks.get(loop)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[loop] = lock
            return lock

    async def resolve(self, seen_generation: int, stale_token: T) -> tuple[T, int]:
        """Return a valid token, calling ``refresh()`` at most once per generation.

        Args:
            seen_generation: The generation observed when the rejected request
                was sent (from :meth:`token_generation`).
            stale_token: The token value that was actually rejected. Used only
                as a secondary check for refreshes performed *outside* this
                coordinator.

        Returns:
            ``(token, generation)`` — a fresh token, either fetched by this
            coroutine or already obtained by the coroutine that won the race,
            plus the generation to carry into the next attempt.

        Raises:
            ReentrantRefreshError: If the current task already holds the
                lock (i.e. ``refresh()`` triggered a nested auth flow on
                the same async client).
            RefreshTimeoutError: If ``timeout`` elapsed while *waiting* for
                another caller's in-flight refresh. The winner's own
                ``refresh()`` is never interrupted.
        """
        if self._holding.get():
            raise ReentrantRefreshError(
                "refresh() triggered the auth flow on the same async client, "
                "causing a reentrant lock acquisition on the same task. "
                + _REENTRANCY_HINT
            )

        lock = self._lock_for_running_loop()
        if self._timeout is None:
            await lock.acquire()
        else:
            try:
                await asyncio.wait_for(lock.acquire(), self._timeout)
            except asyncio.TimeoutError as exc:
                raise RefreshTimeoutError(
                    f"Timed out after {self._timeout}s waiting for an in-flight token refresh"
                ) from exc

        holder = self._holding.set(True)
        try:
            # Double-check: another coroutine may have refreshed while we
            # were suspended waiting for the lock.
            with self._guard:
                if self._generation != seen_generation:
                    return self._get_token(), self._generation
                # Secondary check for refreshes we did not perform (background
                # TTL task, another process writing the shared token store).
                current = self._get_token()
                if current != stale_token:
                    return current, self._generation
                cached = self._last_error
                replay = (
                    cached is not None
                    and time.monotonic() - cached[0] < self._error_cooldown
                )
            if cached is not None and replay:
                # The auth server just failed us; do not pile on.
                raise _replay(cached[1])

            # We are the winner — perform the async refresh.
            try:
                token = await self._refresh()
            except Exception as exc:
                with self._guard:
                    self._last_error = (time.monotonic(), exc)
                raise
            with self._guard:
                self._last_error = None
                self._generation += 1
                generation = self._generation
            return token, generation
        finally:
            self._holding.reset(holder)
            lock.release()
