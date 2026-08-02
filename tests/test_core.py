"""
Tests for SyncCoordinator and AsyncCoordinator (_core.py).

No HTTP libraries are imported here — only pure Python concurrency primitives.
"""

from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

from singleflight_auth._core import AsyncCoordinator, SyncCoordinator

# ---------------------------------------------------------------------------
# SyncCoordinator
# ---------------------------------------------------------------------------


class TestSyncCoordinator:
    """Tests for the thread-safe double-checked locking coordinator."""

    def test_refresh_called_when_token_is_stale(self) -> None:
        """resolve() calls refresh() and returns its value when token matches stale."""
        store = {"token": "old"}

        def get_token() -> str:
            return store["token"]

        def refresh() -> str:
            store["token"] = "new"
            return "new"

        coord = SyncCoordinator(get_token=get_token, refresh=refresh)
        result = coord.resolve(stale_token="old")

        assert result == "new"
        assert store["token"] == "new"

    def test_refresh_not_called_when_token_already_updated(self) -> None:
        """resolve() skips refresh() when get_token() differs from stale_token."""
        store = {"token": "already-fresh"}
        refresh_mock = MagicMock(return_value="should-not-be-called")

        coord = SyncCoordinator(
            get_token=lambda: store["token"],
            refresh=refresh_mock,
        )
        result = coord.resolve(stale_token="old")

        assert result == "already-fresh"
        refresh_mock.assert_not_called()

    def test_exactly_one_refresh_under_concurrent_threads(self) -> None:
        """Only 1 refresh() call occurs when 20 threads race to resolve the same stale token."""
        store = {"token": "stale"}
        refresh_mock = MagicMock()

        def refresh() -> str:
            time.sleep(0.02)  # simulate network latency
            refresh_mock()
            store["token"] = "fresh"
            return "fresh"

        coord = SyncCoordinator(get_token=lambda: store["token"], refresh=refresh)

        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = [executor.submit(coord.resolve, "stale") for _ in range(20)]
            results = [f.result() for f in futures]

        assert all(r == "fresh" for r in results), "Every thread must receive 'fresh'"
        assert refresh_mock.call_count == 1, (
            f"refresh() was called {refresh_mock.call_count} times — must be exactly 1"
        )

    def test_refresh_return_value_propagated(self) -> None:
        """The value returned by refresh() is the value returned by resolve()."""
        coord = SyncCoordinator(
            get_token=lambda: "stale",
            refresh=lambda: "brand-new-token",
        )
        assert coord.resolve(stale_token="stale") == "brand-new-token"

    def test_resolve_with_non_string_tokens(self) -> None:
        """Coordinator works with any comparable type, not just strings."""
        store: dict[str, int] = {"token": 1}

        def refresh() -> int:
            store["token"] = 2
            return 2

        coord = SyncCoordinator(get_token=lambda: store["token"], refresh=refresh)
        result = coord.resolve(stale_token=1)
        assert result == 2


# ---------------------------------------------------------------------------
# AsyncCoordinator
# ---------------------------------------------------------------------------


class TestAsyncCoordinator:
    """Tests for the asyncio-safe double-checked locking coordinator."""

    async def test_refresh_called_when_token_is_stale(self) -> None:
        """resolve() calls refresh() and returns its value when token matches stale."""
        store = {"token": "old"}

        async def refresh() -> str:
            store["token"] = "new"
            return "new"

        coord = AsyncCoordinator(get_token=lambda: store["token"], refresh=refresh)
        result = await coord.resolve(stale_token="old")

        assert result == "new"
        assert store["token"] == "new"

    async def test_refresh_not_called_when_token_already_updated(self) -> None:
        """resolve() skips refresh() when get_token() differs from stale_token."""
        store = {"token": "already-fresh"}
        refresh_called = False

        async def refresh() -> str:
            nonlocal refresh_called
            refresh_called = True
            return "should-not-be-called"

        coord = AsyncCoordinator(
            get_token=lambda: store["token"],
            refresh=refresh,
        )
        result = await coord.resolve(stale_token="old")

        assert result == "already-fresh"
        assert not refresh_called

    async def test_exactly_one_refresh_under_concurrent_coroutines(self) -> None:
        """Only 1 refresh() call occurs when 20 coroutines race to resolve the same stale token."""
        store = {"token": "stale"}
        refresh_call_count = 0

        async def refresh() -> str:
            nonlocal refresh_call_count
            await asyncio.sleep(0.02)  # simulate async network latency
            refresh_call_count += 1
            store["token"] = "fresh"
            return "fresh"

        coord = AsyncCoordinator(get_token=lambda: store["token"], refresh=refresh)

        results = await asyncio.gather(
            *[coord.resolve("stale") for _ in range(20)]
        )

        assert all(r == "fresh" for r in results), "Every coroutine must receive 'fresh'"
        assert refresh_call_count == 1, (
            f"refresh() was called {refresh_call_count} times — must be exactly 1"
        )

    async def test_refresh_return_value_propagated(self) -> None:
        """The value returned by refresh() is the value returned by resolve()."""

        async def refresh() -> str:
            return "brand-new-token"

        coord = AsyncCoordinator(
            get_token=lambda: "stale",
            refresh=refresh,
        )
        assert await coord.resolve(stale_token="stale") == "brand-new-token"

    async def test_lock_is_asyncio_lock_not_threading(self) -> None:
        """AsyncCoordinator must use asyncio.Lock, never threading.Lock."""
        coord = AsyncCoordinator(
            get_token=lambda: "x",
            refresh=lambda: asyncio.coroutine(lambda: "x")(),  # type: ignore[call-arg]
        )
        assert isinstance(coord._lock, asyncio.Lock), (
            "AsyncCoordinator._lock must be asyncio.Lock to avoid blocking the event loop"
        )
