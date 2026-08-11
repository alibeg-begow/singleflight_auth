"""
Regression tests for the issues fixed in 0.2.0.

Two kinds of test live here, and the difference matters when judging them:

*Proofs* pin down a behaviour that was verifiably broken in 0.1.3.  Every one
of them was run against the 0.1.3 source and observed to fail there; a proof
that stays green on the old code is not catching anything and must be fixed or
reclassified.

*Guards* pin down behaviour that 0.1.3 already got right — non-replayable
bodies still rejected, the ``requests`` redirect/cookie/history chain still
intact, the status-code predicate still not buffering.  These pass on both
versions **by design**: they exist so the fixes above cannot quietly break
something that used to work.

Two harness traps are worth knowing about:

* httpx's ``MockTransport`` reads the request body eagerly and hands back an
  already-read response, which hides both body-replay and body-detection bugs.
  These tests use ``pytest-httpserver`` instead.
* The stock ``httpserver`` fixture is **single-threaded**, so it answers one
  request at a time and silently serialises any "N concurrent 401s" scenario —
  waiters never contend, and a locking bug goes unreproduced.  Tests that need
  real contention use the ``threaded_httpserver`` fixture plus a
  :class:`threading.Barrier` to release the whole wave at once.
"""

from __future__ import annotations

import asyncio
import io
import pathlib
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import IO, cast

import httpx
import pytest
import requests
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Request, Response

import singleflight_auth
from singleflight_auth import (
    AsyncSingleFlightAuth,
    MaxRetriesExceededError,
    NonReplayableBodyError,
    ReentrantRefreshError,
    RefreshFailedError,
    RequestsSingleFlightAuth,
    SingleFlightAuth,
)
from singleflight_auth.httpx_auth import _is_replayable_field, _is_replayable_stream
from singleflight_auth.requests_auth import _is_replayable_body

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# K-1: optional extras must really be optional
# ---------------------------------------------------------------------------

_IMPORT_PROBE = """
import sys

class Blocker:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in {blocked!r}:
            raise ImportError("No module named " + name)
        return None

sys.meta_path.insert(0, Blocker())

import singleflight_auth
from singleflight_auth import RefreshFailedError, MaxRetriesExceededError
{body}
print("PROBE-OK")
"""


def _run_probe(blocked: list[str], body: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", _IMPORT_PROBE.format(blocked=blocked, body=body)],
        capture_output=True,
        text=True,
    )


class TestOptionalExtras:
    """`pip install singleflight-auth[httpx]` must be importable without requests."""

    @pytest.mark.parametrize(
        ("blocked", "usable"),
        [
            (["requests"], "singleflight_auth.SingleFlightAuth"),
            (["requests"], "singleflight_auth.AsyncSingleFlightAuth"),
            (["httpx"], "singleflight_auth.RequestsSingleFlightAuth"),
        ],
    )
    def test_package_imports_with_only_one_integration_installed(
        self, blocked: list[str], usable: str
    ) -> None:
        result = _run_probe(blocked, body=f"assert {usable} is not None")
        assert "PROBE-OK" in result.stdout, result.stderr

    def test_package_imports_with_neither_integration_installed(self) -> None:
        result = _run_probe(["httpx", "requests"])
        assert "PROBE-OK" in result.stdout, result.stderr

    def test_missing_extra_raises_actionable_import_error(self) -> None:
        body = (
            "try:\n"
            "    singleflight_auth.SingleFlightAuth\n"
            "except ImportError as exc:\n"
            "    assert 'singleflight-auth[httpx]' in str(exc), str(exc)\n"
            "else:\n"
            "    raise AssertionError('expected ImportError')\n"
        )
        result = _run_probe(["httpx"], body=body)
        assert "PROBE-OK" in result.stdout, result.stderr

    def test_unknown_attribute_still_raises_attribute_error(self) -> None:
        import singleflight_auth

        with pytest.raises(AttributeError):
            getattr(singleflight_auth, "NoSuchThing")  # noqa: B009 — attribute must be dynamic

    def test_dir_lists_the_public_api(self) -> None:
        import singleflight_auth

        assert "SingleFlightAuth" in dir(singleflight_auth)
        assert "RequestsSingleFlightAuth" in dir(singleflight_auth)


# ---------------------------------------------------------------------------
# K-2: a shared async instance must survive more than one event loop
# ---------------------------------------------------------------------------


class TestAsyncInstanceAcrossEventLoops:
    def test_shared_auth_survives_a_second_asyncio_run(
        self, threaded_httpserver: HTTPServer
    ) -> None:
        """The documented 'create once, share everywhere' usage across loops.

        The failure is **contention-only**: an uncontended ``asyncio.Lock``
        takes a fast path that never touches ``_get_loop()``, so the lock is
        not bound to a loop and nothing breaks.  The batch therefore has to
        produce genuinely simultaneous 401s — a barrier in the handler holds
        every request until all of them have arrived, so all ``WAVE`` waiters
        pile onto the lock at once in loop 1 and bind it there.

        Without both the threaded server and the barrier this test passes
        against the buggy 0.1.3 code.
        """
        wave = 10
        store = {"access": "stale"}
        refresh_calls = 0
        gate = threading.Barrier(wave, timeout=15)

        def handler(request: Request) -> Response:
            if request.headers.get("Authorization") == f"Bearer {store['access']}":
                if store["access"] != "stale":
                    return Response(status=200, response=b"ok")
            # Unauthorized: hold every waiter until the whole wave has arrived.
            try:
                gate.wait()
            except threading.BrokenBarrierError:  # pragma: no cover — timeout guard
                pass
            return Response(status=401)

        threaded_httpserver.expect_request("/ep").respond_with_handler(handler)

        async def refresh() -> str:
            nonlocal refresh_calls
            await asyncio.sleep(0.02)
            refresh_calls += 1
            store["access"] = f"fresh-{refresh_calls}"
            return store["access"]

        auth = AsyncSingleFlightAuth(get_token=lambda: store["access"], refresh=refresh)

        async def batch() -> list[int]:
            base = threaded_httpserver.url_for("/")
            async with httpx.AsyncClient(auth=auth, base_url=base) as c:
                responses = await asyncio.gather(*[c.get("/ep") for _ in range(wave)])
            return [r.status_code for r in responses]

        first = asyncio.run(batch())
        assert refresh_calls == 1, "the wave must actually contend, not trickle through"

        store["access"] = "stale"
        gate.reset()
        second = asyncio.run(batch())  # a brand-new event loop, the same auth object

        assert first == [200] * wave
        assert second == [200] * wave, "second event loop must not hit a cross-loop lock error"
        assert refresh_calls == 2


# ---------------------------------------------------------------------------
# K-3: the guarantee must not depend on the token *value* changing
# ---------------------------------------------------------------------------


class TestGuaranteeWithUnchangedTokenValue:
    def test_sync_single_flight_when_refresh_returns_the_same_token(
        self, httpserver: HTTPServer
    ) -> None:
        """Idempotent refresh endpoints return the same token — still exactly one call."""
        refresh_calls = 0
        counter_lock = threading.Lock()
        accept = threading.Event()

        def handler(request: Request) -> Response:
            if accept.is_set():
                return Response(status=200, response=b"ok")
            return Response(status=401)

        httpserver.expect_request("/ep").respond_with_handler(handler)

        def refresh() -> str:
            nonlocal refresh_calls
            time.sleep(0.1)  # real latency, so waiters actually pile up on the lock
            with counter_lock:
                refresh_calls += 1
            accept.set()
            return "constant-token"  # identical on every call

        auth = SingleFlightAuth(get_token=lambda: "constant-token", refresh=refresh)

        with httpx.Client(auth=auth, base_url=httpserver.url_for("/")) as client:
            with ThreadPoolExecutor(max_workers=20) as ex:
                statuses = list(ex.map(lambda _: client.get("/ep").status_code, range(20)))

        assert statuses == [200] * 20
        assert refresh_calls == 1, (
            f"refresh() called {refresh_calls} times — a constant token value "
            f"must not defeat the single-flight guarantee"
        )

    async def test_async_single_flight_when_refresh_returns_the_same_token(
        self, httpserver: HTTPServer
    ) -> None:
        refresh_calls = 0
        accept = threading.Event()

        def handler(request: Request) -> Response:
            if accept.is_set():
                return Response(status=200, response=b"ok")
            return Response(status=401)

        httpserver.expect_request("/ep").respond_with_handler(handler)

        async def refresh() -> str:
            nonlocal refresh_calls
            await asyncio.sleep(0.1)  # real latency, so waiters pile up on the lock
            refresh_calls += 1
            accept.set()
            return "constant-token"

        auth = AsyncSingleFlightAuth(get_token=lambda: "constant-token", refresh=refresh)

        async with httpx.AsyncClient(auth=auth, base_url=httpserver.url_for("/")) as client:
            responses = await asyncio.gather(*[client.get("/ep") for _ in range(20)])

        assert [r.status_code for r in responses] == [200] * 20
        assert refresh_calls == 1

    def test_requests_single_flight_when_refresh_returns_the_same_token(
        self, httpserver: HTTPServer
    ) -> None:
        refresh_calls = 0
        counter_lock = threading.Lock()
        accept = threading.Event()

        def handler(request: Request) -> Response:
            if accept.is_set():
                return Response(status=200, response=b"ok")
            return Response(status=401)

        httpserver.expect_request("/ep").respond_with_handler(handler)

        def refresh() -> str:
            nonlocal refresh_calls
            time.sleep(0.1)  # real latency, so waiters pile up on the lock
            with counter_lock:
                refresh_calls += 1
            accept.set()
            return "constant-token"

        auth = RequestsSingleFlightAuth(get_token=lambda: "constant-token", refresh=refresh)

        def do() -> int:
            session = requests.Session()
            session.auth = auth
            return session.get(httpserver.url_for("/ep")).status_code

        with ThreadPoolExecutor(max_workers=20) as ex:
            statuses = list(ex.map(lambda _: do(), range(20)))

        assert statuses == [200] * 20
        assert refresh_calls == 1


# ---------------------------------------------------------------------------
# K-4: a failing refresh must not become a thundering herd
# ---------------------------------------------------------------------------


class TestFailedRefreshDoesNotStampede:
    def test_sync_failing_refresh_hits_auth_server_once(self, httpserver: HTTPServer) -> None:
        """A down auth server must not receive one refresh per pending 401."""
        refresh_calls = 0
        counter_lock = threading.Lock()
        httpserver.expect_request("/ep").respond_with_data("", status=401)

        def refresh() -> str:
            nonlocal refresh_calls
            time.sleep(0.1)  # waiters must be queued behind us when we fail
            with counter_lock:
                refresh_calls += 1
            raise RuntimeError("auth server returned 503")

        auth = SingleFlightAuth(
            get_token=lambda: "stale",
            refresh=refresh,
            refresh_error_cooldown=5.0,
        )

        with httpx.Client(auth=auth, base_url=httpserver.url_for("/")) as client:
            def attempt() -> str:
                with pytest.raises(RefreshFailedError) as exc_info:
                    client.get("/ep")
                return type(exc_info.value.__cause__).__name__

            with ThreadPoolExecutor(max_workers=20) as ex:
                causes = list(ex.map(lambda _: attempt(), range(20)))

        assert refresh_calls == 1, (
            f"failing refresh() was called {refresh_calls} times for 20 requests — "
            f"the library must not amplify an auth outage"
        )
        assert causes == ["RuntimeError"] * 20, "every waiter still learns why it failed"

    async def test_async_failing_refresh_hits_auth_server_once(
        self, httpserver: HTTPServer
    ) -> None:
        refresh_calls = 0
        httpserver.expect_request("/ep").respond_with_data("", status=401)

        async def refresh() -> str:
            nonlocal refresh_calls
            await asyncio.sleep(0.02)
            refresh_calls += 1
            raise RuntimeError("auth server returned 503")

        auth = AsyncSingleFlightAuth(
            get_token=lambda: "stale",
            refresh=refresh,
            refresh_error_cooldown=5.0,
        )

        async with httpx.AsyncClient(auth=auth, base_url=httpserver.url_for("/")) as client:
            results = await asyncio.gather(
                *[client.get("/ep") for _ in range(20)], return_exceptions=True
            )

        assert refresh_calls == 1
        assert all(isinstance(r, RefreshFailedError) for r in results)

    def test_cooldown_of_zero_restores_retry_every_time(self, httpserver: HTTPServer) -> None:
        """The cooldown is opt-out for callers who want an attempt per request."""
        refresh_calls = 0
        httpserver.expect_request("/ep").respond_with_data("", status=401)

        def refresh() -> str:
            nonlocal refresh_calls
            refresh_calls += 1
            raise RuntimeError("down")

        auth = SingleFlightAuth(
            get_token=lambda: "stale",
            refresh=refresh,
            refresh_error_cooldown=0.0,
        )

        with httpx.Client(auth=auth, base_url=httpserver.url_for("/")) as client:
            for _ in range(3):
                with pytest.raises(RefreshFailedError):
                    client.get("/ep")

        assert refresh_calls == 3


# ---------------------------------------------------------------------------
# Y-1: a sync/async mix-up must not send an unauthenticated request
# ---------------------------------------------------------------------------


class TestSyncAsyncMismatch:
    async def test_sync_auth_with_async_client_raises(self, httpserver: HTTPServer) -> None:
        received: list[str | None] = []

        def handler(request: Request) -> Response:
            received.append(request.headers.get("Authorization"))
            return Response(status=200, response=b"ok")

        httpserver.expect_request("/ep").respond_with_handler(handler)
        auth = SingleFlightAuth(get_token=lambda: "tok", refresh=lambda: "tok2")

        async with httpx.AsyncClient(auth=auth, base_url=httpserver.url_for("/")) as client:
            with pytest.raises(TypeError, match="sync-only"):
                await client.get("/ep")

        assert received == [], "the request must not reach the server unauthenticated"

    def test_async_auth_with_sync_client_raises(self, httpserver: HTTPServer) -> None:
        received: list[str | None] = []

        def handler(request: Request) -> Response:
            received.append(request.headers.get("Authorization"))
            return Response(status=200, response=b"ok")

        httpserver.expect_request("/ep").respond_with_handler(handler)

        async def refresh() -> str:
            return "tok2"

        auth = AsyncSingleFlightAuth(get_token=lambda: "tok", refresh=refresh)

        with httpx.Client(auth=auth, base_url=httpserver.url_for("/")) as client:
            with pytest.raises(TypeError, match="async-only"):
                client.get("/ep")

        assert received == []


# ---------------------------------------------------------------------------
# Y-2: multipart uploads are replayable
# ---------------------------------------------------------------------------


class TestBodyReplayability:
    def _upload_server(self, httpserver: HTTPServer) -> None:
        def handler(request: Request) -> Response:
            if request.headers.get("Authorization") == "Bearer fresh":
                return Response(status=200, response=request.get_data() or b"ok")
            return Response(status=401)

        httpserver.expect_request("/upload").respond_with_handler(handler)

    def _auth(self, store: dict[str, str]) -> SingleFlightAuth:
        def refresh() -> str:
            store["access"] = "fresh"
            return "fresh"

        return SingleFlightAuth(get_token=lambda: store["access"], refresh=refresh)

    def test_multipart_upload_is_retried_after_401(self, httpserver: HTTPServer) -> None:
        """`files=` uploads used to fail with NonReplayableBodyError on every 401."""
        self._upload_server(httpserver)
        store = {"access": "stale"}

        with httpx.Client(auth=self._auth(store), base_url=httpserver.url_for("/")) as client:
            resp = client.post("/upload", files={"f": ("a.txt", io.BytesIO(b"payload"))})

        assert resp.status_code == 200
        assert b"payload" in resp.content, "the file must be re-sent intact, not truncated"

    def test_multipart_with_data_fields_is_retried(self, httpserver: HTTPServer) -> None:
        self._upload_server(httpserver)
        store = {"access": "stale"}

        with httpx.Client(auth=self._auth(store), base_url=httpserver.url_for("/")) as client:
            resp = client.post(
                "/upload",
                data={"field": "value"},
                files={"f": ("a.txt", io.BytesIO(b"payload"))},
            )

        assert resp.status_code == 200
        assert b"value" in resp.content

    async def test_async_multipart_upload_is_retried(self, httpserver: HTTPServer) -> None:
        self._upload_server(httpserver)
        store = {"access": "stale"}

        async def refresh() -> str:
            store["access"] = "fresh"
            return "fresh"

        auth = AsyncSingleFlightAuth(get_token=lambda: store["access"], refresh=refresh)
        async with httpx.AsyncClient(auth=auth, base_url=httpserver.url_for("/")) as client:
            resp = await client.post("/upload", files={"f": ("a.txt", io.BytesIO(b"payload"))})

        assert resp.status_code == 200
        assert b"payload" in resp.content

    def test_non_seekable_multipart_file_still_rejected(self, httpserver: HTTPServer) -> None:
        """A file httpx cannot rewind is genuinely unsafe to replay."""
        self._upload_server(httpserver)
        store = {"access": "stale"}

        class Unseekable(io.RawIOBase):
            def readable(self) -> bool:
                return True

            def seekable(self) -> bool:
                return False

            def read(self, size: int = -1) -> bytes:
                return b""

        unseekable = cast("IO[bytes]", Unseekable())
        with httpx.Client(auth=self._auth(store), base_url=httpserver.url_for("/")) as client:
            with pytest.raises(NonReplayableBodyError):
                client.post("/upload", files={"f": ("a.txt", unseekable)})

    def test_generator_body_still_rejected(self, httpserver: HTTPServer) -> None:
        self._upload_server(httpserver)
        store = {"access": "stale"}

        def chunks() -> object:
            yield b"a"
            yield b"b"

        with httpx.Client(auth=self._auth(store), base_url=httpserver.url_for("/")) as client:
            with pytest.raises(NonReplayableBodyError):
                client.post("/upload", content=chunks())  # type: ignore[arg-type]

    def test_bytes_body_is_retried(self, httpserver: HTTPServer) -> None:
        self._upload_server(httpserver)
        store = {"access": "stale"}

        with httpx.Client(auth=self._auth(store), base_url=httpserver.url_for("/")) as client:
            resp = client.post("/upload", content=b"payload")

        assert resp.status_code == 200
        assert resp.content == b"payload"


# ---------------------------------------------------------------------------
# Y-3: body-based 401 detection
# ---------------------------------------------------------------------------


class TestBodyBasedDetection:
    def test_custom_predicate_can_read_the_response_body(self, httpserver: HTTPServer) -> None:
        """APIs that signal expiry in the body (with a 200!) must be supported."""
        store = {"access": "stale"}

        def handler(request: Request) -> Response:
            if request.headers.get("Authorization") == "Bearer fresh":
                return Response(status=200, response=b'{"data": 1}')
            return Response(status=200, response=b'{"error": "token_expired"}')

        httpserver.expect_request("/ep").respond_with_handler(handler)

        def refresh() -> str:
            store["access"] = "fresh"
            return "fresh"

        auth = SingleFlightAuth(
            get_token=lambda: store["access"],
            refresh=refresh,
            is_unauthorized=lambda r: b"token_expired" in r.content,
        )
        # Nothing is buffered up front — a status-code-only predicate must not
        # pay for a knob it never uses.
        assert auth.requires_response_body is False

        with httpx.Client(auth=auth, base_url=httpserver.url_for("/")) as client:
            resp = client.get("/ep")

        assert resp.json() == {"data": 1}
        assert auth.requires_response_body is True, (
            "after a predicate reaches for the body, later responses are buffered up front"
        )

    async def test_async_custom_predicate_can_read_the_body(
        self, httpserver: HTTPServer
    ) -> None:
        store = {"access": "stale"}

        def handler(request: Request) -> Response:
            if request.headers.get("Authorization") == "Bearer fresh":
                return Response(status=200, response=b'{"data": 1}')
            return Response(status=200, response=b'{"error": "token_expired"}')

        httpserver.expect_request("/ep").respond_with_handler(handler)

        async def refresh() -> str:
            store["access"] = "fresh"
            return "fresh"

        auth = AsyncSingleFlightAuth(
            get_token=lambda: store["access"],
            refresh=refresh,
            is_unauthorized=lambda r: b"token_expired" in r.content,
        )

        async with httpx.AsyncClient(auth=auth, base_url=httpserver.url_for("/")) as client:
            resp = await client.get("/ep")

        assert resp.json() == {"data": 1}

    def test_default_predicate_leaves_streaming_untouched(self) -> None:
        """The status-code default must not force httpx to buffer every body."""
        auth = SingleFlightAuth(get_token=lambda: "t", refresh=lambda: "t2")
        assert auth.requires_response_body is False

    def test_status_code_only_predicate_never_buffers(
        self, httpserver: HTTPServer
    ) -> None:
        """A custom predicate that ignores the body must keep streaming lazy.

        This is the common customisation (``status_code == 403``); buffering
        every response for it would be a silent memory regression for anyone
        downloading with ``client.stream()``.
        """
        store = {"access": "stale"}
        httpserver.expect_request("/ep").respond_with_data("payload", status=200)

        auth = SingleFlightAuth(
            get_token=lambda: store["access"],
            refresh=lambda: "fresh",
            is_unauthorized=lambda r: r.status_code == 403,
        )

        with httpx.Client(auth=auth, base_url=httpserver.url_for("/")) as client:
            with client.stream("GET", "/ep") as resp:
                assert not hasattr(resp, "_content"), "body must not have been buffered"
                assert b"".join(resp.iter_bytes()) == b"payload"

        assert auth.requires_response_body is False

    def test_explicit_true_buffers_up_front(self) -> None:
        auth = SingleFlightAuth(
            get_token=lambda: "t",
            refresh=lambda: "t2",
            requires_response_body=True,
        )
        assert auth.requires_response_body is True

    def test_explicit_false_conflicts_loudly_with_a_body_predicate(
        self, httpserver: HTTPServer
    ) -> None:
        """Opting out and then reading the body must not fail cryptically."""
        httpserver.expect_request("/ep").respond_with_data("nope", status=401)

        auth = SingleFlightAuth(
            get_token=lambda: "stale",
            refresh=lambda: "fresh",
            is_unauthorized=lambda r: b"nope" in r.content,
            requires_response_body=False,
        )

        with httpx.Client(auth=auth, base_url=httpserver.url_for("/")) as client:
            with pytest.raises(RuntimeError, match="requires_response_body"):
                client.get("/ep")


# ---------------------------------------------------------------------------
# Y-4: control exceptions must not be re-wrapped
# ---------------------------------------------------------------------------


class TestReentrantRefreshReachesTheCaller:
    def test_sync_reentrant_refresh(self, httpserver: HTTPServer) -> None:
        """`except ReentrantRefreshError` used to be dead code."""
        httpserver.expect_request("/ep").respond_with_data("", status=401)
        holder: dict[str, httpx.Client] = {}

        def refresh() -> str:
            holder["client"].get("/ep")  # the documented "wrong" usage
            return "new"

        auth = SingleFlightAuth(get_token=lambda: "stale", refresh=refresh)

        with httpx.Client(auth=auth, base_url=httpserver.url_for("/")) as client:
            holder["client"] = client
            with pytest.raises(ReentrantRefreshError):
                client.get("/ep")

    async def test_async_reentrant_refresh(self, httpserver: HTTPServer) -> None:
        httpserver.expect_request("/ep").respond_with_data("", status=401)
        holder: dict[str, httpx.AsyncClient] = {}

        async def refresh() -> str:
            await holder["client"].get("/ep")
            return "new"

        auth = AsyncSingleFlightAuth(get_token=lambda: "stale", refresh=refresh)

        async with httpx.AsyncClient(auth=auth, base_url=httpserver.url_for("/")) as client:
            holder["client"] = client
            with pytest.raises(ReentrantRefreshError):
                await client.get("/ep")

    def test_requests_reentrant_refresh(self, httpserver: HTTPServer) -> None:
        httpserver.expect_request("/ep").respond_with_data("", status=401)
        holder: dict[str, requests.Session] = {}

        def refresh() -> str:
            holder["session"].get(httpserver.url_for("/ep"))
            return "new"

        auth = RequestsSingleFlightAuth(get_token=lambda: "stale", refresh=refresh)
        session = requests.Session()
        session.auth = auth
        holder["session"] = session

        with pytest.raises(ReentrantRefreshError):
            session.get(httpserver.url_for("/ep"))

    def test_reentrant_error_is_not_wrapped(self, httpserver: HTTPServer) -> None:
        """It must be the exception itself, not a RefreshFailedError cause."""
        httpserver.expect_request("/ep").respond_with_data("", status=401)
        holder: dict[str, httpx.Client] = {}

        def refresh() -> str:
            holder["client"].get("/ep")
            return "new"

        auth = SingleFlightAuth(get_token=lambda: "stale", refresh=refresh)

        with httpx.Client(auth=auth, base_url=httpserver.url_for("/")) as client:
            holder["client"] = client
            with pytest.raises(ReentrantRefreshError) as exc_info:
                client.get("/ep")

        assert not isinstance(exc_info.value, RefreshFailedError)


# ---------------------------------------------------------------------------
# O-1: permanent 401s stay inspectable
# ---------------------------------------------------------------------------


class TestPermanent401Handling:
    def test_exception_carries_the_final_response(self, httpserver: HTTPServer) -> None:
        httpserver.expect_request("/ep").respond_with_data(
            "no scope", status=401, headers={"WWW-Authenticate": "Bearer scope=admin"}
        )
        store = {"access": "stale"}

        def refresh() -> str:
            store["access"] = "fresh"
            return "fresh"

        auth = SingleFlightAuth(get_token=lambda: store["access"], refresh=refresh)

        with httpx.Client(auth=auth, base_url=httpserver.url_for("/")) as client:
            with pytest.raises(MaxRetriesExceededError) as exc_info:
                client.get("/ep")

        response = exc_info.value.response
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer scope=admin"
        assert response.text == "no scope"

    def test_raise_on_max_retries_false_returns_the_response(
        self, httpserver: HTTPServer
    ) -> None:
        httpserver.expect_request("/ep").respond_with_data("nope", status=401)
        store = {"access": "stale"}

        def refresh() -> str:
            store["access"] = "fresh"
            return "fresh"

        auth = SingleFlightAuth(
            get_token=lambda: store["access"],
            refresh=refresh,
            raise_on_max_retries=False,
        )

        with httpx.Client(auth=auth, base_url=httpserver.url_for("/")) as client:
            resp = client.get("/ep")

        assert resp.status_code == 401
        assert resp.text == "nope"

    async def test_async_raise_on_max_retries_false(self, httpserver: HTTPServer) -> None:
        httpserver.expect_request("/ep").respond_with_data("nope", status=401)
        store = {"access": "stale"}

        async def refresh() -> str:
            store["access"] = "fresh"
            return "fresh"

        auth = AsyncSingleFlightAuth(
            get_token=lambda: store["access"],
            refresh=refresh,
            raise_on_max_retries=False,
        )

        async with httpx.AsyncClient(auth=auth, base_url=httpserver.url_for("/")) as client:
            resp = await client.get("/ep")

        assert resp.status_code == 401

    def test_requests_raise_on_max_retries_false(self, httpserver: HTTPServer) -> None:
        httpserver.expect_request("/ep").respond_with_data("nope", status=401)
        store = {"access": "stale"}

        def refresh() -> str:
            store["access"] = "fresh"
            return "fresh"

        auth = RequestsSingleFlightAuth(
            get_token=lambda: store["access"],
            refresh=refresh,
            raise_on_max_retries=False,
        )
        session = requests.Session()
        session.auth = auth
        resp = session.get(httpserver.url_for("/ep"))

        assert resp.status_code == 401
        assert resp.text == "nope"

    def test_requests_exception_carries_the_final_response(
        self, httpserver: HTTPServer
    ) -> None:
        httpserver.expect_request("/ep").respond_with_data("no scope", status=401)
        auth = RequestsSingleFlightAuth(get_token=lambda: "stale", refresh=lambda: "fresh")
        session = requests.Session()
        session.auth = auth

        with pytest.raises(MaxRetriesExceededError) as exc_info:
            session.get(httpserver.url_for("/ep"))

        assert exc_info.value.response.status_code == 401
        assert exc_info.value.response.text == "no scope"


# ---------------------------------------------------------------------------
# O-4 / O-5: token formatting
# ---------------------------------------------------------------------------


class TestTokenFormatting:
    def test_empty_token_sends_no_header_instead_of_bearer_none(
        self, httpserver: HTTPServer
    ) -> None:
        """`get_token()` returning None used to put the literal 'Bearer None' on the wire."""
        received: list[str | None] = []
        store: dict[str, str | None] = {"access": None}

        def handler(request: Request) -> Response:
            received.append(request.headers.get("Authorization"))
            if request.headers.get("Authorization") == "Bearer fresh":
                return Response(status=200, response=b"ok")
            return Response(status=401)

        httpserver.expect_request("/ep").respond_with_handler(handler)

        def refresh() -> str:
            store["access"] = "fresh"
            return "fresh"

        auth = SingleFlightAuth(
            get_token=lambda: store["access"],  # type: ignore[arg-type,return-value]
            refresh=refresh,
        )

        with httpx.Client(auth=auth, base_url=httpserver.url_for("/")) as client:
            resp = client.get("/ep")

        assert resp.status_code == 200
        assert received[0] is None, f"sent {received[0]!r} for an empty token"
        assert received[1] == "Bearer fresh"

    @pytest.mark.parametrize(
        ("header_name", "scheme", "expected"),
        [
            ("Authorization", "Token", "Token tok"),
            ("Authorization", "", "tok"),
            ("X-API-Key", "", "tok"),
        ],
    )
    def test_custom_header_and_scheme(
        self, httpserver: HTTPServer, header_name: str, scheme: str, expected: str
    ) -> None:
        received: list[str | None] = []

        def handler(request: Request) -> Response:
            received.append(request.headers.get(header_name))
            return Response(status=200, response=b"ok")

        httpserver.expect_request("/ep").respond_with_handler(handler)

        auth = SingleFlightAuth(
            get_token=lambda: "tok",
            refresh=lambda: "tok2",
            header_name=header_name,
            scheme=scheme,
        )
        with httpx.Client(auth=auth, base_url=httpserver.url_for("/")) as client:
            client.get("/ep")

        assert received == [expected]

    def test_requests_empty_token_sends_no_header(self, httpserver: HTTPServer) -> None:
        received: list[str | None] = []
        store: dict[str, str | None] = {"access": None}

        def handler(request: Request) -> Response:
            received.append(request.headers.get("Authorization"))
            if request.headers.get("Authorization") == "Bearer fresh":
                return Response(status=200, response=b"ok")
            return Response(status=401)

        httpserver.expect_request("/ep").respond_with_handler(handler)

        def refresh() -> str:
            store["access"] = "fresh"
            return "fresh"

        auth = RequestsSingleFlightAuth(
            get_token=lambda: store["access"],  # type: ignore[arg-type,return-value]
            refresh=refresh,
        )
        session = requests.Session()
        session.auth = auth
        resp = session.get(httpserver.url_for("/ep"))

        assert resp.status_code == 200
        assert received[0] is None
        assert received[1] == "Bearer fresh"

    def test_requests_custom_header_and_scheme(self, httpserver: HTTPServer) -> None:
        received: list[str | None] = []

        def handler(request: Request) -> Response:
            received.append(request.headers.get("X-API-Key"))
            return Response(status=200, response=b"ok")

        httpserver.expect_request("/ep").respond_with_handler(handler)

        auth = RequestsSingleFlightAuth(
            get_token=lambda: "tok",
            refresh=lambda: "tok2",
            header_name="X-API-Key",
            scheme="",
        )
        session = requests.Session()
        session.auth = auth
        session.get(httpserver.url_for("/ep"))

        assert received == ["tok"]


# ---------------------------------------------------------------------------
# Replayability predicates (unit level)
# ---------------------------------------------------------------------------


class TestReplayabilityPredicates:
    """Direct coverage of the body-replay decisions behind NonReplayableBodyError."""

    def test_multipart_data_field_is_replayable(self) -> None:
        request = httpx.Request("POST", "http://x/", data={"a": "b"}, files={"f": b"raw"})
        assert _is_replayable_stream(request.stream) is True

    def test_string_backed_file_field_is_replayable(self) -> None:
        """httpx renders str/bytes file content from memory — always replayable."""
        request = httpx.Request("POST", "http://x/", files={"f": ("a.txt", "text")})
        assert _is_replayable_stream(request.stream) is True

    def test_base64_transfer_encoding_is_not_replayable(self) -> None:
        """Older httpx skips the rewind for base64 fields."""

        class Field:
            file = io.BytesIO(b"x")
            headers = {"Content-Transfer-Encoding": "base64"}

        assert _is_replayable_field(Field()) is False

    def test_file_without_seekable_is_not_replayable(self) -> None:
        class Field:
            file = object()
            headers: dict[str, str] = {}

        assert _is_replayable_field(Field()) is False

    def test_data_field_without_file_is_replayable(self) -> None:
        class Field:
            headers: dict[str, str] = {}

        assert _is_replayable_field(Field()) is True

    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            (None, True),
            (b"bytes", True),
            ("str", True),
            (memoryview(b"mv"), True),
            (io.BytesIO(b"x"), False),
            (iter([b"a"]), False),
        ],
    )
    def test_requests_body_predicate(self, body: object, expected: bool) -> None:
        assert _is_replayable_body(body) is expected

    def test_requests_file_body_raises_non_replayable(self, httpserver: HTTPServer) -> None:
        """A file-like `data=` body cannot be rewound by PreparedRequest.copy()."""
        httpserver.expect_request("/upload").respond_with_data("", status=401)

        auth = RequestsSingleFlightAuth(get_token=lambda: "stale", refresh=lambda: "fresh")
        session = requests.Session()
        session.auth = auth

        with pytest.raises(NonReplayableBodyError):
            session.post(httpserver.url_for("/upload"), data=io.BytesIO(b"payload"))


# ---------------------------------------------------------------------------
# max_retries=0 / async parity
# ---------------------------------------------------------------------------


class TestAsyncParity:
    async def test_async_max_retries_zero_returns_the_response(
        self, httpserver: HTTPServer
    ) -> None:
        refresh_calls = 0
        httpserver.expect_request("/ep").respond_with_data("", status=401)

        async def refresh() -> str:
            nonlocal refresh_calls
            refresh_calls += 1
            return "new"

        auth = AsyncSingleFlightAuth(
            get_token=lambda: "token", refresh=refresh, max_retries=0
        )

        async with httpx.AsyncClient(auth=auth, base_url=httpserver.url_for("/")) as client:
            resp = await client.get("/ep")

        assert resp.status_code == 401
        assert refresh_calls == 0

    async def test_async_generator_body_rejected(self, httpserver: HTTPServer) -> None:
        httpserver.expect_request("/upload").respond_with_data("", status=401)

        async def refresh() -> str:
            return "fresh"

        async def chunks() -> object:
            yield b"a"

        auth = AsyncSingleFlightAuth(get_token=lambda: "stale", refresh=refresh)
        async with httpx.AsyncClient(auth=auth, base_url=httpserver.url_for("/")) as client:
            with pytest.raises(NonReplayableBodyError):
                await client.post("/upload", content=chunks())  # type: ignore[arg-type]

    def test_requests_max_retries_zero_returns_the_response(
        self, httpserver: HTTPServer
    ) -> None:
        refresh_calls = 0
        httpserver.expect_request("/ep").respond_with_data("", status=401)

        def refresh() -> str:
            nonlocal refresh_calls
            refresh_calls += 1
            return "new"

        auth = RequestsSingleFlightAuth(
            get_token=lambda: "token", refresh=refresh, max_retries=0
        )
        session = requests.Session()
        session.auth = auth
        resp = session.get(httpserver.url_for("/ep"))

        assert resp.status_code == 401
        assert refresh_calls == 0


# ---------------------------------------------------------------------------
# requests: the delicate hook + connection.send() path must stay intact
# ---------------------------------------------------------------------------


class TestRequestsRedirectAndCookieHandling:
    """The retry re-sends via ``connection.send()``, bypassing Session.send().

    That is the one genuinely subtle piece of this library — history, cookie
    jar updates and redirect following all depend on it being wired correctly.
    These tests pin it down so a refactor cannot quietly break it.
    """

    def test_redirect_after_refresh_is_followed(self, httpserver: HTTPServer) -> None:
        store = {"access": "stale"}

        def guarded(request: Request) -> Response:
            if request.headers.get("Authorization") != "Bearer fresh":
                return Response(status=401)
            return Response(status=302, headers={"Location": httpserver.url_for("/final")})

        httpserver.expect_request("/start").respond_with_handler(guarded)
        httpserver.expect_request("/final").respond_with_data("arrived", status=200)

        def refresh() -> str:
            store["access"] = "fresh"
            return "fresh"

        auth = RequestsSingleFlightAuth(get_token=lambda: store["access"], refresh=refresh)
        session = requests.Session()
        session.auth = auth
        resp = session.get(httpserver.url_for("/start"))

        assert resp.status_code == 200
        assert resp.text == "arrived"
        assert resp.url.endswith("/final")
        # ``Session.resolve_redirects`` overwrites ``resp.history`` with its own
        # chain, so the 401 attempt we recorded on the 302 is dropped once a
        # redirect follows.  That is requests' behaviour — verified identical
        # against 0.1.3 — not something the retry path loses.  The 401 *is*
        # preserved when no redirect intervenes; see the two-retry test below.
        assert [r.status_code for r in resp.history] == [302]

    def test_set_cookie_from_the_retried_response_reaches_the_jar(
        self, httpserver: HTTPServer
    ) -> None:
        store = {"access": "stale"}

        def guarded(request: Request) -> Response:
            if request.headers.get("Authorization") != "Bearer fresh":
                return Response(status=401)
            return Response(status=200, response=b"ok", headers={"Set-Cookie": "sid=abc123"})

        httpserver.expect_request("/ep").respond_with_handler(guarded)

        def refresh() -> str:
            store["access"] = "fresh"
            return "fresh"

        auth = RequestsSingleFlightAuth(get_token=lambda: store["access"], refresh=refresh)
        session = requests.Session()
        session.auth = auth
        resp = session.get(httpserver.url_for("/ep"))

        assert resp.status_code == 200
        assert resp.cookies.get("sid") == "abc123"
        assert session.cookies.get("sid") == "abc123", "the session jar must be updated"

    def test_history_chain_is_preserved_across_two_retries(
        self, httpserver: HTTPServer
    ) -> None:
        store = {"access": "v0"}
        versions = iter(["v1", "v2"])

        def guarded(request: Request) -> Response:
            if request.headers.get("Authorization") == "Bearer v2":
                return Response(status=200, response=b"ok")
            return Response(status=401)

        httpserver.expect_request("/ep").respond_with_handler(guarded)

        def refresh() -> str:
            store["access"] = next(versions)
            return store["access"]

        auth = RequestsSingleFlightAuth(
            get_token=lambda: store["access"], refresh=refresh, max_retries=2
        )
        session = requests.Session()
        session.auth = auth
        resp = session.get(httpserver.url_for("/ep"))

        assert resp.status_code == 200
        assert [r.status_code for r in resp.history] == [401, 401]


class TestExplicitResponseBodyBuffering:
    """``requires_response_body=True`` must buffer up front on both flows."""

    def test_sync_explicit_true_buffers_before_the_predicate(
        self, httpserver: HTTPServer
    ) -> None:
        seen: list[bool] = []
        httpserver.expect_request("/ep").respond_with_data("payload", status=200)

        def predicate(response: httpx.Response) -> bool:
            seen.append(hasattr(response, "_content"))
            return False

        auth = SingleFlightAuth(
            get_token=lambda: "t",
            refresh=lambda: "t2",
            is_unauthorized=predicate,
            requires_response_body=True,
        )
        with httpx.Client(auth=auth, base_url=httpserver.url_for("/")) as client:
            assert client.get("/ep").text == "payload"

        assert seen == [True], "the body must already be buffered when the predicate runs"

    async def test_async_explicit_true_buffers_before_the_predicate(
        self, httpserver: HTTPServer
    ) -> None:
        seen: list[bool] = []
        httpserver.expect_request("/ep").respond_with_data("payload", status=200)

        def predicate(response: httpx.Response) -> bool:
            seen.append(hasattr(response, "_content"))
            return False

        async def refresh() -> str:
            return "t2"

        auth = AsyncSingleFlightAuth(
            get_token=lambda: "t",
            refresh=refresh,
            is_unauthorized=predicate,
            requires_response_body=True,
        )
        async with httpx.AsyncClient(auth=auth, base_url=httpserver.url_for("/")) as client:
            resp = await client.get("/ep")

        assert resp.text == "payload"
        assert seen == [True]

    async def test_async_explicit_false_conflicts_loudly(
        self, httpserver: HTTPServer
    ) -> None:
        httpserver.expect_request("/ep").respond_with_data("nope", status=401)

        async def refresh() -> str:
            return "fresh"

        auth = AsyncSingleFlightAuth(
            get_token=lambda: "stale",
            refresh=refresh,
            is_unauthorized=lambda r: b"nope" in r.content,
            requires_response_body=False,
        )
        async with httpx.AsyncClient(auth=auth, base_url=httpserver.url_for("/")) as client:
            with pytest.raises(RuntimeError, match="requires_response_body"):
                await client.get("/ep")


class TestAutoResponseBodyProbeLimits:
    """The lazy body probe is the newest machinery here — pin down its edges.

    Each of these documents a real, measured consequence of auto-detection.
    ``requires_response_body=True`` avoids all of them deterministically.
    """

    @staticmethod
    def _serve(httpserver: HTTPServer) -> None:
        def handler(request: Request) -> Response:
            if request.headers.get("Authorization") == "Bearer fresh":
                return Response(status=200, response=b'{"data": 1}')
            return Response(status=200, response=b'{"error": "token_expired"}')

        httpserver.expect_request("/ep").respond_with_handler(handler)

    def test_predicate_double_fires_once_then_settles(
        self, httpserver: HTTPServer
    ) -> None:
        """A side-effecting predicate fires twice on the first body-reading response.

        The probe has to run the predicate once to discover that it needs the
        body, then again on the buffered response.  That happens once per auth
        instance, not once per request.
        """
        store = {"access": "stale"}
        self._serve(httpserver)
        calls: list[int] = []

        def predicate(response: httpx.Response) -> bool:
            calls.append(1)  # a metrics counter would double-count here
            return b"token_expired" in response.content

        def refresh() -> str:
            store["access"] = "fresh"
            return "fresh"

        auth = SingleFlightAuth(
            get_token=lambda: store["access"], refresh=refresh, is_unauthorized=predicate
        )
        base = httpserver.url_for("/")

        with httpx.Client(auth=auth, base_url=base) as client:
            assert client.get("/ep").json() == {"data": 1}
        # Two responses, but the first one cost an extra probe call.
        assert len(calls) == 3

        calls.clear()
        store["access"] = "stale"
        with httpx.Client(auth=auth, base_url=base) as client:
            assert client.get("/ep").json() == {"data": 1}
        assert len(calls) == 2, "once the flag is set, exactly one call per response"

    def test_explicit_true_never_double_fires(self, httpserver: HTTPServer) -> None:
        """The deterministic escape hatch: no probe, so no duplicate call."""
        store = {"access": "stale"}
        self._serve(httpserver)
        calls: list[int] = []

        def predicate(response: httpx.Response) -> bool:
            calls.append(1)
            return b"token_expired" in response.content

        def refresh() -> str:
            store["access"] = "fresh"
            return "fresh"

        auth = SingleFlightAuth(
            get_token=lambda: store["access"],
            refresh=refresh,
            is_unauthorized=predicate,
            requires_response_body=True,
        )
        with httpx.Client(auth=auth, base_url=httpserver.url_for("/")) as client:
            assert client.get("/ep").json() == {"data": 1}

        assert len(calls) == 2, "one call per response from the very first request"

    async def test_async_probe_uses_aread_not_read(self, httpserver: HTTPServer) -> None:
        """The async probe must await ``aread()``.

        ``Response.read()`` iterates the stream synchronously, which cannot work
        on an async transport — so a green result here *is* the proof that the
        async branch does not fall back to the sync call.
        """
        store = {"access": "stale"}
        self._serve(httpserver)

        async def refresh() -> str:
            store["access"] = "fresh"
            return "fresh"

        auth = AsyncSingleFlightAuth(
            get_token=lambda: store["access"],
            refresh=refresh,
            is_unauthorized=lambda r: b"token_expired" in r.content,
        )
        async with httpx.AsyncClient(auth=auth, base_url=httpserver.url_for("/")) as client:
            resp = await client.get("/ep")

        assert resp.json() == {"data": 1}
        assert auth.requires_response_body is True

    def test_predicate_that_swallows_the_signal_fails_silently(
        self, httpserver: HTTPServer
    ) -> None:
        """Known limitation: catching ResponseNotRead yourself defeats detection.

        The probe learns "this predicate wants the body" from the exception
        escaping it.  A defensive ``except Exception: return False`` eats that
        signal, so the predicate sees an unread body, reports "authorized", and
        no refresh happens.  Pinned here so the behaviour is documented rather
        than discovered in production — the remedy is asserted below it.
        """
        store = {"access": "stale"}
        self._serve(httpserver)

        def swallowing(response: httpx.Response) -> bool:
            try:
                return b"token_expired" in response.content
            except Exception:  # noqa: BLE001 — the whole point of the test
                return False

        def refresh() -> str:
            store["access"] = "fresh"
            return "fresh"

        auth = SingleFlightAuth(
            get_token=lambda: store["access"], refresh=refresh, is_unauthorized=swallowing
        )
        with httpx.Client(auth=auth, base_url=httpserver.url_for("/")) as client:
            resp = client.get("/ep")

        assert resp.json() == {"error": "token_expired"}, "detection silently no-ops"
        assert store["access"] == "stale", "no refresh was triggered"

        # The documented remedy: state the requirement instead of probing for it.
        store["access"] = "stale"
        fixed = SingleFlightAuth(
            get_token=lambda: store["access"],
            refresh=refresh,
            is_unauthorized=swallowing,
            requires_response_body=True,
        )
        with httpx.Client(auth=fixed, base_url=httpserver.url_for("/")) as client:
            assert client.get("/ep").json() == {"data": 1}
        assert store["access"] == "fresh"


class TestVersionIsImportable:
    """``__version__`` must not depend on installation metadata."""

    def test_version_is_a_plain_literal_not_installation_metadata(self) -> None:
        """A plain checkout must be able to report its version.

        ``pyproject.toml`` reads the version *from* this literal via
        ``hatch.version`` (a regex, no import), so the dependency points one
        way only.  Switching to ``importlib.metadata.version()`` would raise
        ``PackageNotFoundError`` on an uninstalled checkout — and this suite,
        which always runs against an installed package, would never notice.
        """
        source = (_REPO_ROOT / "src" / "singleflight_auth" / "__init__.py").read_text(
            encoding="utf-8"
        )

        assert "importlib.metadata" not in source
        assert re.search(r'^__version__ = "[^"]+"$', source, re.MULTILINE), (
            "__version__ must stay a plain literal"
        )

    def test_version_is_declared_dynamic_and_not_duplicated(self) -> None:
        """Parsed as text so the test itself runs on 3.10 (``tomllib`` is 3.11+)."""
        config = (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")

        assert 'dynamic = ["version"]' in config
        assert 'path = "src/singleflight_auth/__init__.py"' in config
        assert not re.search(r"^version = ", config, re.MULTILINE), (
            "the version must live in __init__.py only, never in pyproject.toml"
        )

    def test_version_is_readable_from_an_uninstalled_checkout(self) -> None:
        """Import straight from ``src/`` with the installed package shadowed."""
        probe = (
            "import sys; sys.path.insert(0, r'{src}')\n"
            "import singleflight_auth as s\n"
            "assert s.__file__.startswith(r'{src}'), s.__file__\n"
            "print(s.__version__)\n"
        ).format(src=str(_REPO_ROOT / "src"))
        result = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == singleflight_auth.__version__
