"""
Tests for SingleFlightAuth (httpx sync).

Uses pytest-httpserver to spin up a real local HTTP server — no mocking
of the auth logic itself.
"""

from __future__ import annotations

import httpx
import pytest
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Request, Response

from singleflight_auth import MaxRetriesExceededError, RefreshFailedError, SingleFlightAuth

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_token_store(initial: str = "stale-token") -> dict[str, str]:
    return {"access": initial}


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestSingleFlightAuthHappyPath:
    def test_initial_token_injected(self, httpserver: HTTPServer) -> None:
        """Authorization header is set from get_token() on the first request."""
        received_headers: list[str] = []

        def handler(request: Request) -> Response:
            received_headers.append(request.headers.get("Authorization", ""))
            return Response(status=200, response=b"ok")

        httpserver.expect_request("/data").respond_with_handler(handler)
        store = make_token_store("my-token")
        auth = SingleFlightAuth(get_token=lambda: store["access"], refresh=lambda: "unused")

        with httpx.Client(auth=auth, base_url=httpserver.url_for("/")) as client:
            resp = client.get("/data")

        assert resp.status_code == 200
        assert received_headers[0] == "Bearer my-token"

    def test_refresh_on_401_and_retry_succeeds(self, httpserver: HTTPServer) -> None:
        """On 401 the coordinator refreshes and the retry with the new token succeeds."""
        store = make_token_store("expired")
        refresh_calls = 0

        def handler(request: Request) -> Response:
            auth = request.headers.get("Authorization", "")
            if auth == "Bearer fresh":
                return Response(status=200, response=b"ok")
            return Response(status=401)

        httpserver.expect_request("/protected").respond_with_handler(handler)

        def refresh() -> str:
            nonlocal refresh_calls
            refresh_calls += 1
            store["access"] = "fresh"
            return "fresh"

        auth = SingleFlightAuth(get_token=lambda: store["access"], refresh=refresh)

        with httpx.Client(auth=auth, base_url=httpserver.url_for("/")) as client:
            resp = client.get("/protected")

        assert resp.status_code == 200
        assert refresh_calls == 1


# ---------------------------------------------------------------------------
# Error scenarios
# ---------------------------------------------------------------------------


class TestSingleFlightAuthErrors:
    def test_max_retries_exceeded_raises(self, httpserver: HTTPServer) -> None:
        """MaxRetriesExceededError is raised when server always returns 401."""
        httpserver.expect_request("/locked").respond_with_data("", status=401)

        store = make_token_store("token")
        auth = SingleFlightAuth(
            get_token=lambda: store["access"],
            refresh=lambda: "new-token",
            max_retries=1,
        )

        with httpx.Client(auth=auth, base_url=httpserver.url_for("/")) as client:
            with pytest.raises(MaxRetriesExceededError):
                client.get("/locked")

    def test_refresh_failure_raises_refresh_failed_error(self, httpserver: HTTPServer) -> None:
        """RefreshFailedError is raised (with original cause) when refresh() raises."""
        httpserver.expect_request("/locked").respond_with_data("", status=401)

        def bad_refresh() -> str:
            raise RuntimeError("network error")

        auth = SingleFlightAuth(
            get_token=lambda: "stale",
            refresh=bad_refresh,
        )

        with httpx.Client(auth=auth, base_url=httpserver.url_for("/")) as client:
            with pytest.raises(RefreshFailedError) as exc_info:
                client.get("/locked")

        assert isinstance(exc_info.value.__cause__, RuntimeError)

    def test_custom_is_unauthorized_predicate(self, httpserver: HTTPServer) -> None:
        """is_unauthorized can be customised (e.g. to treat 403 as trigger)."""
        store = make_token_store("stale")
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

        auth = SingleFlightAuth(
            get_token=lambda: store["access"],
            refresh=refresh,
            is_unauthorized=lambda r: r.status_code == 403,
        )

        with httpx.Client(auth=auth, base_url=httpserver.url_for("/")) as client:
            resp = client.get("/resource")

        assert resp.status_code == 200
        assert refresh_calls == 1

    def test_non_401_responses_are_not_retried(self, httpserver: HTTPServer) -> None:
        """500 responses do not trigger refresh."""
        store = make_token_store("token")
        refresh_calls = 0

        httpserver.expect_request("/error").respond_with_data("", status=500)

        def refresh() -> str:
            nonlocal refresh_calls
            refresh_calls += 1
            return "new"

        auth = SingleFlightAuth(get_token=lambda: store["access"], refresh=refresh)

        with httpx.Client(auth=auth, base_url=httpserver.url_for("/")) as client:
            resp = client.get("/error")

        assert resp.status_code == 500
        assert refresh_calls == 0
