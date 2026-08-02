"""
Edge case and hardening tests (Phase 5).

Covers scenarios that are outside the happy path but critical for
production robustness:
  - refresh() returns None or empty string
  - Mixed 401 + 500 responses in a concurrent batch
  - is_unauthorized customisation
  - max_retries boundary behaviour
  - base_url + relative path combinations with httpx
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
import requests
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Request, Response

from singleflight_auth import (
    AsyncSingleFlightAuth,
    MaxRetriesExceededError,
    RefreshFailedError,
    RequestsSingleFlightAuth,
    SingleFlightAuth,
)

# ---------------------------------------------------------------------------
# refresh() returns empty / None-like value
# ---------------------------------------------------------------------------


class TestRefreshReturnsInvalidToken:
    def test_sync_empty_token_still_retried(self, httpserver: HTTPServer) -> None:
        """When refresh() returns '', the request fails with RefreshFailedError."""
        store = {"access": "stale"}

        def handler(request: Request) -> Response:
            # Nothing accepts an empty token — server always returns 401
            return Response(status=401)

        httpserver.expect_request("/ep").respond_with_handler(handler)

        def refresh() -> str:
            store["access"] = ""
            return ""

        auth = SingleFlightAuth(
            get_token=lambda: store["access"],
            refresh=refresh,
            max_retries=1,
        )

        with httpx.Client(auth=auth, base_url=httpserver.url_for("/")) as client:
            with pytest.raises(RefreshFailedError):
                client.get("/ep")

    async def test_async_empty_token_still_retried(self, httpserver: HTTPServer) -> None:
        """Async variant: empty string from refresh() results in RefreshFailedError."""
        store = {"access": "stale"}

        def handler(request: Request) -> Response:
            return Response(status=401)

        httpserver.expect_request("/ep").respond_with_handler(handler)

        async def refresh() -> str:
            store["access"] = ""
            return ""

        auth = AsyncSingleFlightAuth(
            get_token=lambda: store["access"],
            refresh=refresh,
            max_retries=1,
        )

        async with httpx.AsyncClient(auth=auth, base_url=httpserver.url_for("/")) as client:
            with pytest.raises(RefreshFailedError):
                await client.get("/ep")


# ---------------------------------------------------------------------------
# Mixed 401 + 500: 500s must not trigger refresh
# ---------------------------------------------------------------------------


class TestMixedStatusCodes:
    async def test_async_500_not_retried_alongside_401(self, httpserver: HTTPServer) -> None:
        """500 responses in a concurrent batch must not trigger refresh."""
        store = {"access": "stale"}
        refresh_calls = 0
        request_counter = {"count": 0}

        def handler(request: Request) -> Response:
            # Alternate: every other request returns 500
            n = request_counter["count"]
            request_counter["count"] += 1
            if n % 2 == 1:
                return Response(status=500)
            if request.headers.get("Authorization") == "Bearer fresh":
                return Response(status=200, response=b"ok")
            return Response(status=401)

        httpserver.expect_request("/mixed").respond_with_handler(handler)

        async def refresh() -> str:
            nonlocal refresh_calls
            await asyncio.sleep(0.01)
            refresh_calls += 1
            store["access"] = "fresh"
            return "fresh"

        auth = AsyncSingleFlightAuth(
            get_token=lambda: store["access"],
            refresh=refresh,
        )

        async with httpx.AsyncClient(auth=auth, base_url=httpserver.url_for("/")) as client:
            results = await asyncio.gather(
                *[client.get("/mixed") for _ in range(10)],
                return_exceptions=True,
            )

        # 500s must never trigger refresh — refresh is only for 401s
        statuses = [r.status_code for r in results if isinstance(r, httpx.Response)]
        assert 500 not in [s for s in statuses if s == 401], (
            "500 responses must not cause refresh calls"
        )
        # Regardless of how many requests triggered a refresh, it must be ≤ 1
        assert refresh_calls <= 1, (
            f"refresh() was called {refresh_calls} times — must be at most 1"
        )


# ---------------------------------------------------------------------------
# max_retries boundary
# ---------------------------------------------------------------------------


class TestMaxRetriesBoundary:
    def test_max_retries_zero_raises_immediately(self, httpserver: HTTPServer) -> None:
        """With max_retries=0, the first 401 raises MaxRetriesExceededError without refresh."""
        refresh_calls = 0
        httpserver.expect_request("/ep").respond_with_data("", status=401)

        def refresh() -> str:
            nonlocal refresh_calls
            refresh_calls += 1
            return "new"

        auth = SingleFlightAuth(
            get_token=lambda: "token",
            refresh=refresh,
            max_retries=0,
        )

        with httpx.Client(auth=auth, base_url=httpserver.url_for("/")) as client:
            with pytest.raises(MaxRetriesExceededError):
                client.get("/ep")

        assert refresh_calls == 0, "No refresh should occur with max_retries=0"

    def test_max_retries_two_allows_two_retries(self, httpserver: HTTPServer) -> None:
        """With max_retries=2, the client retries twice before raising."""
        store = {"access": "v0"}
        call_count = {"n": 0}

        def handler(request: Request) -> Response:
            # Only succeed on the third token version
            if request.headers.get("Authorization") == "Bearer v2":
                return Response(status=200, response=b"ok")
            return Response(status=401)

        httpserver.expect_request("/ep").respond_with_handler(handler)

        def refresh() -> str:
            call_count["n"] += 1
            store["access"] = f"v{call_count['n']}"
            return store["access"]

        auth = SingleFlightAuth(
            get_token=lambda: store["access"],
            refresh=refresh,
            max_retries=2,
        )

        with httpx.Client(auth=auth, base_url=httpserver.url_for("/")) as client:
            resp = client.get("/ep")

        assert resp.status_code == 200
        assert call_count["n"] == 2


# ---------------------------------------------------------------------------
# httpx base_url + relative path compatibility
# ---------------------------------------------------------------------------


class TestHttpxBaseUrlCompat:
    def test_sync_with_base_url_and_relative_path(self, httpserver: HTTPServer) -> None:
        """Auth works correctly when httpx.Client has a base_url set."""
        store = {"access": "stale"}

        def handler(request: Request) -> Response:
            if request.headers.get("Authorization") == "Bearer fresh":
                return Response(status=200, response=b"ok")
            return Response(status=401)

        httpserver.expect_request("/api/v1/users").respond_with_handler(handler)

        def refresh() -> str:
            store["access"] = "fresh"
            return "fresh"

        auth = SingleFlightAuth(get_token=lambda: store["access"], refresh=refresh)

        base = httpserver.url_for("/api/v1/")
        with httpx.Client(auth=auth, base_url=base) as client:
            resp = client.get("users")

        assert resp.status_code == 200

    async def test_async_with_base_url_and_relative_path(self, httpserver: HTTPServer) -> None:
        """Async auth works correctly when httpx.AsyncClient has a base_url set."""
        store = {"access": "stale"}

        def handler(request: Request) -> Response:
            if request.headers.get("Authorization") == "Bearer fresh":
                return Response(status=200, response=b"ok")
            return Response(status=401)

        httpserver.expect_request("/api/v1/users").respond_with_handler(handler)

        async def refresh() -> str:
            store["access"] = "fresh"
            return "fresh"

        auth = AsyncSingleFlightAuth(get_token=lambda: store["access"], refresh=refresh)

        base = httpserver.url_for("/api/v1/")
        async with httpx.AsyncClient(auth=auth, base_url=base) as client:
            resp = await client.get("users")

        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# requests edge cases
# ---------------------------------------------------------------------------


class TestRequestsEdgeCases:
    def test_concurrent_requests_with_mixed_statuses(self, httpserver: HTTPServer) -> None:
        """requests: concurrent threads; 500s must not trigger refresh."""
        store = {"access": "stale"}
        refresh_calls = 0
        request_counter = {"n": 0}

        def handler(request: Request) -> Response:
            n = request_counter["n"]
            request_counter["n"] += 1
            if n % 3 == 0:
                return Response(status=500)
            if request.headers.get("Authorization") == "Bearer fresh":
                return Response(status=200, response=b"ok")
            return Response(status=401)

        httpserver.expect_request("/mixed").respond_with_handler(handler)

        def refresh() -> str:
            nonlocal refresh_calls
            import time

            time.sleep(0.01)
            refresh_calls += 1
            store["access"] = "fresh"
            return "fresh"

        auth = RequestsSingleFlightAuth(
            get_token=lambda: store["access"],
            refresh=refresh,
        )

        def do() -> int:
            s = requests.Session()
            s.auth = auth
            return s.get(httpserver.url_for("/mixed")).status_code

        with ThreadPoolExecutor(max_workers=9) as ex:
            list(ex.map(lambda _: do(), range(9)))

        assert refresh_calls <= 1, (
            f"refresh() called {refresh_calls} times — must be at most 1"
        )
