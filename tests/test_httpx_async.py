"""
Tests for AsyncSingleFlightAuth (httpx async).

Uses pytest-httpserver (real local server) + pytest-asyncio.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Request, Response

from singleflight_auth import AsyncSingleFlightAuth, MaxRetriesExceededError, RefreshFailedError

# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestAsyncSingleFlightAuthHappyPath:
    async def test_initial_token_injected(self, httpserver: HTTPServer) -> None:
        """Authorization header is set from get_token() on the first request."""
        received: list[str] = []

        def handler(request: Request) -> Response:
            received.append(request.headers.get("Authorization", ""))
            return Response(status=200, response=b"ok")

        httpserver.expect_request("/data").respond_with_handler(handler)
        store = {"access": "my-async-token"}

        auth = AsyncSingleFlightAuth(
            get_token=lambda: store["access"],
            refresh=lambda: asyncio.coroutine(lambda: "unused")(),  # type: ignore[call-arg]
        )

        async with httpx.AsyncClient(auth=auth, base_url=httpserver.url_for("/")) as client:
            resp = await client.get("/data")

        assert resp.status_code == 200
        assert received[0] == "Bearer my-async-token"

    async def test_refresh_on_401_and_retry_succeeds(self, httpserver: HTTPServer) -> None:
        """On 401 the async coordinator refreshes and the retry with new token succeeds."""
        store = {"access": "expired"}
        refresh_calls = 0

        def handler(request: Request) -> Response:
            if request.headers.get("Authorization") == "Bearer fresh":
                return Response(status=200, response=b"ok")
            return Response(status=401)

        httpserver.expect_request("/protected").respond_with_handler(handler)

        async def refresh() -> str:
            nonlocal refresh_calls
            refresh_calls += 1
            store["access"] = "fresh"
            return "fresh"

        auth = AsyncSingleFlightAuth(get_token=lambda: store["access"], refresh=refresh)

        async with httpx.AsyncClient(auth=auth, base_url=httpserver.url_for("/")) as client:
            resp = await client.get("/protected")

        assert resp.status_code == 200
        assert refresh_calls == 1


# ---------------------------------------------------------------------------
# Error scenarios
# ---------------------------------------------------------------------------


class TestAsyncSingleFlightAuthErrors:
    async def test_max_retries_exceeded_raises(self, httpserver: HTTPServer) -> None:
        """MaxRetriesExceededError is raised when server always returns 401."""
        httpserver.expect_request("/locked").respond_with_data("", status=401)
        store = {"access": "token"}

        async def refresh() -> str:
            return "new-token"

        auth = AsyncSingleFlightAuth(
            get_token=lambda: store["access"],
            refresh=refresh,
            max_retries=1,
        )

        async with httpx.AsyncClient(auth=auth, base_url=httpserver.url_for("/")) as client:
            with pytest.raises(MaxRetriesExceededError):
                await client.get("/locked")

    async def test_refresh_failure_raises_refresh_failed_error(
        self, httpserver: HTTPServer
    ) -> None:
        """RefreshFailedError is raised (with original cause) when refresh() raises."""
        httpserver.expect_request("/locked").respond_with_data("", status=401)

        async def bad_refresh() -> str:
            raise RuntimeError("async network error")

        auth = AsyncSingleFlightAuth(
            get_token=lambda: "stale",
            refresh=bad_refresh,
        )

        async with httpx.AsyncClient(auth=auth, base_url=httpserver.url_for("/")) as client:
            with pytest.raises(RefreshFailedError) as exc_info:
                await client.get("/locked")

        assert isinstance(exc_info.value.__cause__, RuntimeError)

    async def test_custom_is_unauthorized_predicate(self, httpserver: HTTPServer) -> None:
        """is_unauthorized can be customised to trigger on 403."""
        store = {"access": "stale"}
        refresh_calls = 0

        def handler(request: Request) -> Response:
            if request.headers.get("Authorization") == "Bearer fresh":
                return Response(status=200, response=b"ok")
            return Response(status=403)

        httpserver.expect_request("/resource").respond_with_handler(handler)

        async def refresh() -> str:
            nonlocal refresh_calls
            refresh_calls += 1
            store["access"] = "fresh"
            return "fresh"

        auth = AsyncSingleFlightAuth(
            get_token=lambda: store["access"],
            refresh=refresh,
            is_unauthorized=lambda r: r.status_code == 403,
        )

        async with httpx.AsyncClient(auth=auth, base_url=httpserver.url_for("/")) as client:
            resp = await client.get("/resource")

        assert resp.status_code == 200
        assert refresh_calls == 1

    async def test_non_401_responses_not_retried(self, httpserver: HTTPServer) -> None:
        """500 responses do not trigger refresh."""
        refresh_calls = 0
        httpserver.expect_request("/error").respond_with_data("", status=500)

        async def refresh() -> str:
            nonlocal refresh_calls
            refresh_calls += 1
            return "new"

        auth = AsyncSingleFlightAuth(get_token=lambda: "token", refresh=refresh)

        async with httpx.AsyncClient(auth=auth, base_url=httpserver.url_for("/")) as client:
            resp = await client.get("/error")

        assert resp.status_code == 500
        assert refresh_calls == 0
