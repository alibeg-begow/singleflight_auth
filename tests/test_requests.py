"""
Tests for RequestsSingleFlightAuth (requests library).

Uses pytest-httpserver for a real local HTTP server.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

import pytest
import requests
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Request, Response

from singleflight_auth import MaxRetriesExceededError, RefreshFailedError, RequestsSingleFlightAuth

# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestRequestsSingleFlightAuthHappyPath:
    def test_initial_token_injected(self, httpserver: HTTPServer) -> None:
        """Authorization header is set from get_token() on the initial request."""
        received: list[str] = []

        def handler(request: Request) -> Response:
            received.append(request.headers.get("Authorization", ""))
            return Response(status=200, response=b"ok")

        httpserver.expect_request("/data").respond_with_handler(handler)
        store = {"access": "my-requests-token"}

        auth = RequestsSingleFlightAuth(
            get_token=lambda: store["access"],
            refresh=lambda: "unused",
        )

        session = requests.Session()
        session.auth = auth
        resp = session.get(httpserver.url_for("/data"))

        assert resp.status_code == 200
        assert received[0] == "Bearer my-requests-token"

    def test_refresh_on_401_and_retry_succeeds(self, httpserver: HTTPServer) -> None:
        """On 401 the coordinator refreshes and the retry with new token succeeds."""
        store = {"access": "expired"}
        refresh_calls = 0

        def handler(request: Request) -> Response:
            if request.headers.get("Authorization") == "Bearer fresh":
                return Response(status=200, response=b"ok")
            return Response(status=401)

        httpserver.expect_request("/protected").respond_with_handler(handler)

        def refresh() -> str:
            nonlocal refresh_calls
            refresh_calls += 1
            store["access"] = "fresh"
            return "fresh"

        auth = RequestsSingleFlightAuth(
            get_token=lambda: store["access"],
            refresh=refresh,
        )

        session = requests.Session()
        session.auth = auth
        resp = session.get(httpserver.url_for("/protected"))

        assert resp.status_code == 200
        assert refresh_calls == 1


# ---------------------------------------------------------------------------
# Error scenarios
# ---------------------------------------------------------------------------


class TestRequestsSingleFlightAuthErrors:
    def test_max_retries_exceeded_raises(self, httpserver: HTTPServer) -> None:
        """MaxRetriesExceededError is raised when server always returns 401."""
        httpserver.expect_request("/locked").respond_with_data("", status=401)
        store = {"access": "token"}

        auth = RequestsSingleFlightAuth(
            get_token=lambda: store["access"],
            refresh=lambda: "new",
            max_retries=1,
        )

        session = requests.Session()
        session.auth = auth

        with pytest.raises(MaxRetriesExceededError):
            session.get(httpserver.url_for("/locked"))

    def test_refresh_failure_raises_refresh_failed_error(self, httpserver: HTTPServer) -> None:
        """RefreshFailedError is raised (with original cause) when refresh() raises."""
        httpserver.expect_request("/locked").respond_with_data("", status=401)

        def bad_refresh() -> str:
            raise ValueError("credentials expired")

        auth = RequestsSingleFlightAuth(
            get_token=lambda: "stale",
            refresh=bad_refresh,
        )

        session = requests.Session()
        session.auth = auth

        with pytest.raises(RefreshFailedError) as exc_info:
            session.get(httpserver.url_for("/locked"))

        assert isinstance(exc_info.value.__cause__, ValueError)

    def test_non_401_not_retried(self, httpserver: HTTPServer) -> None:
        """500 responses do not trigger refresh."""
        refresh_calls = 0
        httpserver.expect_request("/error").respond_with_data("", status=500)

        def refresh() -> str:
            nonlocal refresh_calls
            refresh_calls += 1
            return "new"

        auth = RequestsSingleFlightAuth(get_token=lambda: "token", refresh=refresh)
        session = requests.Session()
        session.auth = auth
        resp = session.get(httpserver.url_for("/error"))

        assert resp.status_code == 500
        assert refresh_calls == 0

    def test_custom_is_unauthorized_predicate(self, httpserver: HTTPServer) -> None:
        """is_unauthorized can be customised (e.g. trigger on 403)."""
        store = {"access": "stale"}
        refresh_calls = 0

        def handler(request: Request) -> Response:
            if request.headers.get("Authorization") == "Bearer fresh":
                return Response(status=200, response=b"ok")
            return Response(status=403)

        httpserver.expect_request("/resource").respond_with_handler(handler)

        def refresh() -> str:
            nonlocal refresh_calls
            refresh_calls += 1
            store["access"] = "fresh"
            return "fresh"

        auth = RequestsSingleFlightAuth(
            get_token=lambda: store["access"],
            refresh=refresh,
            is_unauthorized=lambda r: r.status_code == 403,
        )

        session = requests.Session()
        session.auth = auth
        resp = session.get(httpserver.url_for("/resource"))

        assert resp.status_code == 200
        assert refresh_calls == 1


# ---------------------------------------------------------------------------
# Concurrency stress test (requests / threading)
# ---------------------------------------------------------------------------


class TestRequestsConcurrencyStress:
    def test_only_one_refresh_under_20_concurrent_401s(self, httpserver: HTTPServer) -> None:
        """20 concurrent threads each hitting a 401 must trigger refresh exactly once."""
        store = {"token": "stale"}
        refresh_call_count = 0

        def handler(request: Request) -> Response:
            if (
                request.headers.get("Authorization") == f"Bearer {store['token']}"
                and store["token"] == "fresh"
            ):
                return Response(status=200, response=b"ok")
            return Response(status=401)

        httpserver.expect_request("/protected").respond_with_handler(handler)

        def refresh() -> str:
            nonlocal refresh_call_count
            time.sleep(0.05)  # simulate real network latency
            refresh_call_count += 1
            store["token"] = "fresh"
            return "fresh"

        auth = RequestsSingleFlightAuth(
            get_token=lambda: store["token"],
            refresh=refresh,
        )

        def make_request() -> int:
            session = requests.Session()
            session.auth = auth
            return session.get(httpserver.url_for("/protected")).status_code

        with ThreadPoolExecutor(max_workers=20) as executor:
            statuses = list(executor.map(lambda _: make_request(), range(20)))

        assert all(s == 200 for s in statuses), f"All requests must succeed: {statuses}"
        assert refresh_call_count == 1, (
            f"refresh() called {refresh_call_count} times — must be exactly 1"
        )
