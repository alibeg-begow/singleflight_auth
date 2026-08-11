"""
Tests for SyncCoordinator and AsyncCoordinator (_core.py).

No HTTP libraries are imported here — only pure Python concurrency primitives.
"""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

import pytest

from singleflight_auth import (
    ReentrantRefreshError,
    RefreshFailedError,
    RefreshTimeoutError,
)
from singleflight_auth._core import AsyncCoordinator, SyncCoordinator

# Cooldown timings for the failure-replay tests.  Kept deliberately generous:
# the original 50 ms window left a 10 ms margin, which a loaded CI runner (or a
# laptop building venvs in the background) blows straight through, turning a
# correct implementation into a red build.
COOLDOWN = 0.3
PAST_COOLDOWN = COOLDOWN * 3

# ---------------------------------------------------------------------------
# SyncCoordinator
# ---------------------------------------------------------------------------


class TestSyncCoordinator:
    """Tests for the thread-safe double-checked locking coordinator."""

    def test_refresh_called_when_generation_is_stale(self) -> None:
        """resolve() calls refresh() when the caller's generation is current."""
        store = {"token": "old"}

        def get_token() -> str:
            return store["token"]

        def refresh() -> str:
            store["token"] = "new"
            return "new"

        coord: SyncCoordinator[str] = SyncCoordinator(get_token=get_token, refresh=refresh)
        token, generation = coord.resolve(coord.token_generation(), stale_token="old")

        assert token == "new"
        assert store["token"] == "new"
        assert generation == 1

    def test_refresh_not_called_when_generation_already_advanced(self) -> None:
        """resolve() skips refresh() when somebody else already bumped the generation."""
        store = {"token": "already-fresh"}
        refresh_mock = MagicMock(return_value="should-not-be-called")

        coord: SyncCoordinator[str] = SyncCoordinator(
            get_token=lambda: store["token"],
            refresh=refresh_mock,
        )
        stale_generation = coord.token_generation()
        coord.resolve(stale_generation, stale_token="already-fresh")  # someone else refreshed
        refresh_mock.reset_mock()

        token, _ = coord.resolve(stale_generation, stale_token="already-fresh")

        assert token == "already-fresh"
        refresh_mock.assert_not_called()

    def test_exactly_one_refresh_under_concurrent_threads(self) -> None:
        """Only 1 refresh() call occurs when 20 threads race on the same generation."""
        store = {"token": "stale"}
        refresh_mock = MagicMock()

        def refresh() -> str:
            time.sleep(0.02)  # simulate network latency
            refresh_mock()
            store["token"] = "fresh"
            return "fresh"

        coord: SyncCoordinator[str] = SyncCoordinator(
            get_token=lambda: store["token"], refresh=refresh
        )
        generation = coord.token_generation()

        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = [executor.submit(coord.resolve, generation, "stale") for _ in range(20)]
            results = [f.result()[0] for f in futures]

        assert all(r == "fresh" for r in results), "Every thread must receive 'fresh'"
        assert refresh_mock.call_count == 1, (
            f"refresh() was called {refresh_mock.call_count} times — must be exactly 1"
        )

    def test_exactly_one_refresh_when_token_value_never_changes(self) -> None:
        """The guarantee must not depend on refresh() returning a *different* token.

        Idempotent/cached refresh endpoints, disabled rotation and fixed
        staging tokens all return the same string.  A value comparison would
        degrade to one refresh per waiter here.
        """
        refresh_calls = 0
        counter_lock = threading.Lock()

        def refresh() -> str:
            nonlocal refresh_calls
            time.sleep(0.02)
            with counter_lock:
                refresh_calls += 1
            return "constant-token"  # identical every time

        coord: SyncCoordinator[str] = SyncCoordinator(
            get_token=lambda: "constant-token", refresh=refresh
        )
        generation = coord.token_generation()

        with ThreadPoolExecutor(max_workers=30) as executor:
            futures = [
                executor.submit(coord.resolve, generation, "constant-token")
                for _ in range(30)
            ]
            results = [f.result()[0] for f in futures]

        assert all(r == "constant-token" for r in results)
        assert refresh_calls == 1, (
            f"refresh() called {refresh_calls} times for a constant token — must be exactly 1"
        )

    def test_failed_refresh_is_not_retried_by_every_waiter(self) -> None:
        """A failing refresh() must not be re-attempted once per pending request."""
        refresh_calls = 0
        counter_lock = threading.Lock()

        def refresh() -> str:
            nonlocal refresh_calls
            time.sleep(0.02)
            with counter_lock:
                refresh_calls += 1
            raise RuntimeError("auth server is down")

        coord: SyncCoordinator[str] = SyncCoordinator(
            get_token=lambda: "stale", refresh=refresh, error_cooldown=5.0
        )
        generation = coord.token_generation()

        def attempt() -> str:
            try:
                coord.resolve(generation, stale_token="stale")
            except RuntimeError as exc:
                return str(exc)
            return "no error"

        with ThreadPoolExecutor(max_workers=30) as executor:
            messages = list(executor.map(lambda _: attempt(), range(30)))

        assert refresh_calls == 1, (
            f"failing refresh() hit the auth server {refresh_calls} times — must be 1"
        )
        assert all(m == "auth server is down" for m in messages), messages

    def test_cooldown_expiry_allows_a_new_attempt(self) -> None:
        """Once the cooldown window passes, refresh() is tried again."""
        refresh_calls = 0

        def refresh() -> str:
            nonlocal refresh_calls
            refresh_calls += 1
            raise RuntimeError("still down")

        coord: SyncCoordinator[str] = SyncCoordinator(
            get_token=lambda: "stale", refresh=refresh, error_cooldown=COOLDOWN
        )
        generation = coord.token_generation()

        for _ in range(2):
            with pytest.raises(RuntimeError):
                coord.resolve(generation, stale_token="stale")
        assert refresh_calls == 1, "second call inside the window must be served from cache"

        time.sleep(PAST_COOLDOWN)
        with pytest.raises(RuntimeError):
            coord.resolve(generation, stale_token="stale")
        assert refresh_calls == 2, "after the cooldown a fresh attempt must be made"

    def test_cooldown_cleared_after_a_success(self) -> None:
        """A successful refresh clears the cached failure."""
        attempts = 0

        def refresh() -> str:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("transient")
            return f"token-{attempts}"

        coord: SyncCoordinator[str] = SyncCoordinator(
            get_token=lambda: "stale", refresh=refresh, error_cooldown=COOLDOWN
        )
        with pytest.raises(RuntimeError):
            coord.resolve(coord.token_generation(), stale_token="stale")

        time.sleep(PAST_COOLDOWN)
        token, _ = coord.resolve(coord.token_generation(), stale_token="stale")
        assert token == "token-2"

        # A later generation-stale call must refresh again, not replay the old error.
        token, _ = coord.resolve(coord.token_generation(), stale_token="stale")
        assert token == "token-3"

    def test_reentrant_refresh_raises_instead_of_deadlocking(self) -> None:
        """refresh() re-entering resolve() on the same thread is reported, not hung."""
        coord: SyncCoordinator[str] = SyncCoordinator(
            get_token=lambda: "stale",
            refresh=lambda: coord.resolve(0, stale_token="stale")[0],
        )
        with pytest.raises(ReentrantRefreshError):
            coord.resolve(coord.token_generation(), stale_token="stale")

    def test_holder_flag_is_released_after_a_failed_refresh(self) -> None:
        """The reentrancy flag is per-thread and cleared even when refresh() raises."""
        coord: SyncCoordinator[str] = SyncCoordinator(
            get_token=lambda: "stale",
            refresh=MagicMock(side_effect=RuntimeError("boom")),
            error_cooldown=0.0,
        )
        for _ in range(2):
            with pytest.raises(RuntimeError):
                coord.resolve(coord.token_generation(), stale_token="stale")

    def test_timeout_raises_refresh_wait_timeout_error(self) -> None:
        """A hung refresh() must not park every other thread forever."""
        started = threading.Event()

        def slow_refresh() -> str:
            started.set()
            time.sleep(0.5)
            return "fresh"

        coord: SyncCoordinator[str] = SyncCoordinator(
            get_token=lambda: "stale", refresh=slow_refresh, timeout=0.05
        )
        generation = coord.token_generation()

        winner = threading.Thread(target=coord.resolve, args=(generation, "stale"))
        winner.start()
        assert started.wait(timeout=2.0)

        with pytest.raises(RefreshTimeoutError):
            coord.resolve(generation, stale_token="stale")

        winner.join()

    def test_refresh_return_value_propagated(self) -> None:
        """The value returned by refresh() is the value returned by resolve()."""
        coord: SyncCoordinator[str] = SyncCoordinator(
            get_token=lambda: "stale",
            refresh=lambda: "brand-new-token",
        )
        token, _ = coord.resolve(coord.token_generation(), stale_token="stale")
        assert token == "brand-new-token"

    def test_resolve_with_non_string_tokens(self) -> None:
        """Coordinator works with any token type, not just strings."""
        store: dict[str, int] = {"token": 1}

        def refresh() -> int:
            store["token"] = 2
            return 2

        coord: SyncCoordinator[int] = SyncCoordinator(
            get_token=lambda: store["token"], refresh=refresh
        )
        token, _ = coord.resolve(coord.token_generation(), stale_token=1)
        assert token == 2


# ---------------------------------------------------------------------------
# AsyncCoordinator
# ---------------------------------------------------------------------------


class TestAsyncCoordinator:
    """Tests for the asyncio-safe double-checked locking coordinator."""

    async def test_refresh_called_when_generation_is_stale(self) -> None:
        """resolve() calls refresh() when the caller's generation is current."""
        store = {"token": "old"}

        async def refresh() -> str:
            store["token"] = "new"
            return "new"

        coord: AsyncCoordinator[str] = AsyncCoordinator(
            get_token=lambda: store["token"], refresh=refresh
        )
        token, generation = await coord.resolve(coord.token_generation(), stale_token="old")

        assert token == "new"
        assert store["token"] == "new"
        assert generation == 1

    async def test_refresh_not_called_when_generation_already_advanced(self) -> None:
        """resolve() skips refresh() when the generation moved on."""
        store = {"token": "already-fresh"}
        refresh_called = 0

        async def refresh() -> str:
            nonlocal refresh_called
            refresh_called += 1
            return "already-fresh"

        coord: AsyncCoordinator[str] = AsyncCoordinator(
            get_token=lambda: store["token"],
            refresh=refresh,
        )
        stale_generation = coord.token_generation()
        await coord.resolve(stale_generation, stale_token="already-fresh")
        assert refresh_called == 1

        token, _ = await coord.resolve(stale_generation, stale_token="already-fresh")

        assert token == "already-fresh"
        assert refresh_called == 1, "the second waiter must not refresh again"

    async def test_exactly_one_refresh_under_concurrent_coroutines(self) -> None:
        """Only 1 refresh() call occurs when 20 coroutines race on the same generation."""
        store = {"token": "stale"}
        refresh_call_count = 0

        async def refresh() -> str:
            nonlocal refresh_call_count
            await asyncio.sleep(0.02)  # simulate async network latency
            refresh_call_count += 1
            store["token"] = "fresh"
            return "fresh"

        coord: AsyncCoordinator[str] = AsyncCoordinator(
            get_token=lambda: store["token"], refresh=refresh
        )
        generation = coord.token_generation()

        results = await asyncio.gather(
            *[coord.resolve(generation, "stale") for _ in range(20)]
        )

        assert all(r[0] == "fresh" for r in results), "Every coroutine must receive 'fresh'"
        assert refresh_call_count == 1, (
            f"refresh() was called {refresh_call_count} times — must be exactly 1"
        )

    async def test_exactly_one_refresh_when_token_value_never_changes(self) -> None:
        """Async guarantee must not depend on the token *value* changing."""
        refresh_calls = 0

        async def refresh() -> str:
            nonlocal refresh_calls
            await asyncio.sleep(0.02)
            refresh_calls += 1
            return "constant-token"

        coord: AsyncCoordinator[str] = AsyncCoordinator(
            get_token=lambda: "constant-token", refresh=refresh
        )
        generation = coord.token_generation()

        await asyncio.gather(
            *[coord.resolve(generation, "constant-token") for _ in range(30)]
        )

        assert refresh_calls == 1, (
            f"refresh() called {refresh_calls} times for a constant token — must be exactly 1"
        )

    async def test_failed_refresh_is_not_retried_by_every_waiter(self) -> None:
        """A failing async refresh() must not be re-attempted once per waiter."""
        refresh_calls = 0

        async def refresh() -> str:
            nonlocal refresh_calls
            await asyncio.sleep(0.02)
            refresh_calls += 1
            raise RuntimeError("auth server is down")

        coord: AsyncCoordinator[str] = AsyncCoordinator(
            get_token=lambda: "stale", refresh=refresh, error_cooldown=5.0
        )
        generation = coord.token_generation()

        results = await asyncio.gather(
            *[coord.resolve(generation, "stale") for _ in range(30)], return_exceptions=True
        )

        assert refresh_calls == 1, (
            f"failing refresh() hit the auth server {refresh_calls} times — must be 1"
        )
        assert all(isinstance(r, RuntimeError) for r in results)
        assert all(str(r) == "auth server is down" for r in results)

    async def test_reentrant_refresh_raises_instead_of_deadlocking(self) -> None:
        """refresh() re-entering resolve() in the same task is reported, not hung."""

        async def refresh() -> str:
            token, _ = await coord.resolve(0, stale_token="stale")
            return token

        coord: AsyncCoordinator[str] = AsyncCoordinator(
            get_token=lambda: "stale", refresh=refresh
        )
        with pytest.raises(ReentrantRefreshError):
            await coord.resolve(coord.token_generation(), stale_token="stale")

    async def test_timeout_raises_refresh_wait_timeout_error(self) -> None:
        """A hung refresh() must not park every other coroutine forever."""

        async def slow_refresh() -> str:
            await asyncio.sleep(0.5)
            return "fresh"

        coord: AsyncCoordinator[str] = AsyncCoordinator(
            get_token=lambda: "stale", refresh=slow_refresh, timeout=0.05
        )
        generation = coord.token_generation()

        winner = asyncio.create_task(coord.resolve(generation, "stale"))
        await asyncio.sleep(0.01)  # let the winner take the lock

        with pytest.raises(RefreshTimeoutError):
            await coord.resolve(generation, stale_token="stale")

        await winner

    def test_same_instance_works_across_separate_event_loops(self) -> None:
        """One shared instance must survive repeated asyncio.run() calls.

        asyncio.Lock binds to the loop that first contends it; a single
        process-wide auth object routinely outlives one loop (pytest-asyncio,
        notebooks, CLI batch jobs, thread-per-loop servers).
        """
        store = {"token": "stale"}
        refresh_calls = 0

        async def refresh() -> str:
            nonlocal refresh_calls
            await asyncio.sleep(0.02)
            refresh_calls += 1
            store["token"] = f"fresh-{refresh_calls}"
            return store["token"]

        coord: AsyncCoordinator[str] = AsyncCoordinator(
            get_token=lambda: store["token"], refresh=refresh
        )

        async def batch() -> list[tuple[str, int]]:
            # Snapshot what a real caller would have sent, exactly as the auth
            # flows do: generation first, then the token it is about to use.
            generation = coord.token_generation()
            sent = store["token"]
            return await asyncio.gather(
                *[coord.resolve(generation, sent) for _ in range(10)]
            )

        first = asyncio.run(batch())
        second = asyncio.run(batch())  # brand-new event loop, same coordinator

        assert all(r[0] == "fresh-1" for r in first), "loop 1 must not raise or diverge"
        assert all(r[0] == "fresh-2" for r in second), (
            "loop 2 must work on the same instance — this is where the "
            "'bound to a different event loop' RuntimeError used to appear"
        )
        assert refresh_calls == 2, "one refresh per batch, no cross-loop lock error"

    async def test_refresh_return_value_propagated(self) -> None:
        """The value returned by refresh() is the value returned by resolve()."""

        async def refresh() -> str:
            return "brand-new-token"

        coord: AsyncCoordinator[str] = AsyncCoordinator(
            get_token=lambda: "stale",
            refresh=refresh,
        )
        token, _ = await coord.resolve(coord.token_generation(), stale_token="stale")
        assert token == "brand-new-token"

    async def test_lock_is_asyncio_lock_not_threading(self) -> None:
        """AsyncCoordinator must wait on an asyncio.Lock, never a threading.Lock."""

        async def refresh() -> str:
            return "x"

        coord: AsyncCoordinator[str] = AsyncCoordinator(get_token=lambda: "x", refresh=refresh)
        lock = coord._lock_for_running_loop()
        assert isinstance(lock, asyncio.Lock), (
            "AsyncCoordinator must use asyncio.Lock to avoid blocking the event loop"
        )
        assert coord._lock_for_running_loop() is lock, "one lock per loop, reused"


# ---------------------------------------------------------------------------
# Invariants called out during review
# ---------------------------------------------------------------------------


class TestCoordinatorInvariants:
    """Direct checks for the internals that are easy to get subtly wrong."""

    def test_external_refresh_is_honoured_without_calling_refresh(self) -> None:
        """A token renewed outside the coordinator must not trigger a refresh.

        The generation only moves when *this* coordinator refreshes.  A pure
        generation check would make a background TTL thread or a sibling
        process's renewal invisible, costing a redundant refresh per 401.
        """
        store = {"token": "v1"}
        refresh_mock = MagicMock(return_value="v-refreshed")
        coord: SyncCoordinator[str] = SyncCoordinator(
            get_token=lambda: store["token"], refresh=refresh_mock
        )
        generation = coord.token_generation()

        store["token"] = "v2"  # renewed by somebody else entirely
        token, gen = coord.resolve(generation, stale_token="v1")

        assert token == "v2"
        refresh_mock.assert_not_called()
        assert gen == generation, "an external renewal must not bump our generation"

    async def test_async_external_refresh_is_honoured(self) -> None:
        store = {"token": "v1"}
        calls = 0

        async def refresh() -> str:
            nonlocal calls
            calls += 1
            return "v-refreshed"

        coord: AsyncCoordinator[str] = AsyncCoordinator(
            get_token=lambda: store["token"], refresh=refresh
        )
        generation = coord.token_generation()
        store["token"] = "v2"

        token, _ = await coord.resolve(generation, stale_token="v1")

        assert token == "v2"
        assert calls == 0

    def test_replayed_error_is_a_distinct_object_with_the_same_cause(self) -> None:
        """Waiters get a copy, so the cached traceback cannot grow unboundedly."""
        root = ValueError("root cause")

        def refresh() -> str:
            raise RefreshFailedError("refresh blew up") from root

        coord: SyncCoordinator[str] = SyncCoordinator(
            get_token=lambda: "stale", refresh=refresh, error_cooldown=5.0
        )
        generation = coord.token_generation()

        seen: list[RefreshFailedError] = []
        for _ in range(3):
            with pytest.raises(RefreshFailedError) as exc_info:
                coord.resolve(generation, stale_token="stale")
            seen.append(exc_info.value)

        assert len({id(e) for e in seen}) == 3, "each waiter must get its own instance"
        assert all(str(e) == "refresh blew up" for e in seen)
        assert all(e.__cause__ is root for e in seen), "the cause chain must survive the copy"

    def test_async_lock_registry_is_guarded_against_concurrent_threads(self) -> None:
        """Thread-per-loop servers create loops concurrently — one lock each, no loss.

        ``WeakKeyDictionary`` is not thread-safe, so the registry is mutated
        under a ``threading.Lock``; without it two threads racing on the same
        loop could each install a lock and silently split the single-flight
        group in two.
        """
        loops: list[asyncio.AbstractEventLoop] = []
        locks: list[asyncio.Lock] = []
        registry_lock = threading.Lock()
        coord: AsyncCoordinator[str] = AsyncCoordinator(
            get_token=lambda: "t", refresh=MagicMock()
        )
        start = threading.Barrier(12, timeout=15)

        def worker() -> None:
            async def grab() -> None:
                start.wait()
                lock = coord._lock_for_running_loop()
                again = coord._lock_for_running_loop()
                assert again is lock, "the same loop must always get the same lock"
                with registry_lock:
                    loops.append(asyncio.get_running_loop())
                    locks.append(lock)

            asyncio.run(grab())

        threads = [threading.Thread(target=worker) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(locks) == 12
        assert len({id(loop) for loop in loops}) == 12, "each thread had its own loop"
        assert len({id(lock) for lock in locks}) == 12, "each loop got its own lock"

    async def test_holder_flag_is_reset_not_cleared(self) -> None:
        """The ContextVar is restored with its token, so no state leaks out."""
        coord: AsyncCoordinator[str] = AsyncCoordinator(
            get_token=lambda: "stale", refresh=MagicMock(side_effect=RuntimeError("boom"))
        )
        outer = coord._holding.set(True)  # pretend an outer frame owned it
        try:
            with pytest.raises(ReentrantRefreshError):
                await coord.resolve(coord.token_generation(), stale_token="stale")
            assert coord._holding.get() is True, "resolve() must not clobber outer state"
        finally:
            coord._holding.reset(outer)
        assert coord._holding.get() is False

    async def test_async_cooldown_cleared_after_a_success(self) -> None:
        """A recovery inside the cooldown window must not keep failing callers."""
        attempts = 0

        async def refresh() -> str:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("transient")
            return f"token-{attempts}"

        coord: AsyncCoordinator[str] = AsyncCoordinator(
            get_token=lambda: "stale", refresh=refresh, error_cooldown=COOLDOWN
        )
        with pytest.raises(RuntimeError):
            await coord.resolve(coord.token_generation(), stale_token="stale")

        await asyncio.sleep(PAST_COOLDOWN)
        token, _ = await coord.resolve(coord.token_generation(), stale_token="stale")
        assert token == "token-2"

        # Immediately afterwards: no stale error may resurface.
        token, _ = await coord.resolve(coord.token_generation(), stale_token="stale")
        assert token == "token-3"

    def test_timeout_bounds_waiting_only_not_the_winners_refresh(self) -> None:
        """The winner's own refresh() runs to completion; only waiters time out."""
        started = threading.Event()

        def slow_refresh() -> str:
            started.set()
            time.sleep(0.3)
            return "fresh"

        coord: SyncCoordinator[str] = SyncCoordinator(
            get_token=lambda: "stale", refresh=slow_refresh, timeout=0.05
        )
        generation = coord.token_generation()
        result: list[str] = []

        winner = threading.Thread(
            target=lambda: result.append(coord.resolve(generation, "stale")[0])
        )
        winner.start()
        assert started.wait(timeout=2.0)

        with pytest.raises(RefreshTimeoutError):
            coord.resolve(generation, stale_token="stale")

        winner.join(timeout=5.0)
        assert result == ["fresh"], "the winner must finish, not be aborted by the timeout"
