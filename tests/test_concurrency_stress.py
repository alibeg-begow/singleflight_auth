"""
Concurrency stress tests — the "proof" that singleflight-auth works.

These tests verify the core promise: N concurrent 401 responses produce
exactly 1 refresh() call, regardless of which HTTP library is used.

Per plan Section 7:
  - Real latency (asyncio.sleep / time.sleep) is added to make races deterministic.
  - call_count is always explicitly asserted — a naive implementation that lets
    every request refresh independently would also return 200s but fail the
    call_count == 1 assertion.
  - At least 20-50 concurrent requests are used to reliably trigger race conditions.
"""

from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Request, Response

from singleflight_auth import AsyncSingleFlightAuth, SingleFlightAuth

# ---------------------------------------------------------------------------
# Async stress test (the canonical "proof" from plan §6 Faz 3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_only_one_refresh_under_50_concurrent_401s(httpserver: HTTPServer) -> None:
    """50 concurrent async requests all hitting 401 must trigger refresh exactly once.

    This is the canonical proof test from plan Section 6 / Phase 3.
    Failure here means the single-flight guarantee is broken.
    """
    state = {"token": "expired", "refresh_calls": 0}

    def handler(request: Request) -> Response:
        auth_header = request.headers.get("Authorization", "")
        # Accept only if the token is 'fresh' AND it matches the bearer value
        if auth_header == f"Bearer {state['token']}" and state["token"] == "fresh":
            return Response(status=200, response=b"ok")
        return Response(status=401)

    httpserver.expect_request("/protected").respond_with_handler(handler)

    async def refresh() -> str:
        await asyncio.sleep(0.05)  # simulate real async network latency
        state["refresh_calls"] += 1
        state["token"] = "fresh"
        return "fresh"

    auth = AsyncSingleFlightAuth(
        get_token=lambda: state["token"],
        refresh=refresh,
    )

    async with httpx.AsyncClient(auth=auth, base_url=httpserver.url_for("/")) as client:
        responses = await asyncio.gather(*[client.get("/protected") for _ in range(50)])

    assert all(r.status_code == 200 for r in responses), (
        f"All 50 responses must be 200; got: {[r.status_code for r in responses]}"
    )
    assert state["refresh_calls"] == 1, (
        f"refresh() was called {state['refresh_calls']} times — must be exactly 1. "
        "The single-flight guarantee is broken."
    )


# ---------------------------------------------------------------------------
# Sync stress test (ThreadPoolExecutor, 50 threads)
# ---------------------------------------------------------------------------


def test_only_one_refresh_under_50_concurrent_401s_sync(httpserver: HTTPServer) -> None:
    """50 concurrent sync threads all hitting 401 must trigger refresh exactly once."""
    state = {"token": "expired", "refresh_calls": 0}

    def handler(request: Request) -> Response:
        auth_header = request.headers.get("Authorization", "")
        if auth_header == f"Bearer {state['token']}" and state["token"] == "fresh":
            return Response(status=200, response=b"ok")
        return Response(status=401)

    httpserver.expect_request("/protected").respond_with_handler(handler)

    def refresh() -> str:
        time.sleep(0.05)  # simulate real network latency
        state["refresh_calls"] += 1
        state["token"] = "fresh"
        return "fresh"

    auth = SingleFlightAuth(
        get_token=lambda: state["token"],
        refresh=refresh,
    )

    def do_request() -> int:
        with httpx.Client(auth=auth, base_url=httpserver.url_for("/")) as client:
            return client.get("/protected").status_code

    with ThreadPoolExecutor(max_workers=50) as executor:
        statuses = list(executor.map(lambda _: do_request(), range(50)))

    assert all(s == 200 for s in statuses), (
        f"All 50 responses must be 200; got: {statuses}"
    )
    assert state["refresh_calls"] == 1, (
        f"refresh() was called {state['refresh_calls']} times — must be exactly 1. "
        "The single-flight guarantee is broken."
    )


# ---------------------------------------------------------------------------
# Repeated stress: same token, multiple sequential waves
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_wave_after_token_expires_again(httpserver: HTTPServer) -> None:
    """After the first refresh wave, if the token expires again, refresh fires once more."""
    state = {"token": "stale-v1", "refresh_calls": 0, "valid_token": "stale-v2", "next_version": 2}

    def handler(request: Request) -> Response:
        if request.headers.get("Authorization") == f"Bearer {state['valid_token']}":
            return Response(status=200, response=b"ok")
        return Response(status=401)

    httpserver.expect_request("/api").respond_with_handler(handler)

    async def refresh() -> str:
        await asyncio.sleep(0.02)
        state["refresh_calls"] += 1
        new_token = f"stale-v{state['next_version']}"
        state["next_version"] += 1
        state["token"] = new_token
        return new_token

    auth = AsyncSingleFlightAuth(
        get_token=lambda: state["token"],
        refresh=refresh,
    )

    async with httpx.AsyncClient(auth=auth, base_url=httpserver.url_for("/")) as client:
        # Wave 1: 20 concurrent requests on stale-v1
        await asyncio.gather(*[client.get("/api") for _ in range(20)])
        first_wave_calls = state["refresh_calls"]

        # Simulate token expiring again
        state["token"] = "stale-v2"
        state["valid_token"] = "stale-v3"

        # Wave 2: 20 more concurrent requests on stale-v2
        await asyncio.gather(*[client.get("/api") for _ in range(20)])

    assert first_wave_calls == 1, f"Wave 1 must have exactly 1 refresh; got {first_wave_calls}"
    assert state["refresh_calls"] == 2, (
        f"Total refresh calls must be 2 (one per wave); got {state['refresh_calls']}"
    )
