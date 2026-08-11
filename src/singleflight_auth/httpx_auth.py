"""
httpx integration for singleflight-auth.

Provides two auth classes:

* :class:`SingleFlightAuth`       — for :class:`httpx.Client` (sync)
* :class:`AsyncSingleFlightAuth`  — for :class:`httpx.AsyncClient` (async)

**Why sync_auth_flow / async_auth_flow are overridden separately:**

``httpx.Auth`` defaults to routing both sync and async clients through the
same ``auth_flow`` generator.  That generator is a plain Python generator —
it cannot ``await`` anything.  If we put a ``threading.Lock`` inside it, the
async client would freeze the entire event loop while blocked.  If we put an
``asyncio.Lock`` inside it we cannot ``await`` the acquisition.

The solution (and the officially supported httpx pattern) is to override
``sync_auth_flow`` and ``async_auth_flow`` **separately**:

* ``sync_auth_flow`` is a regular generator (``def``) and uses
  :class:`threading.Lock` via :class:`~._core.SyncCoordinator`.
* ``async_auth_flow`` is an async generator (``async def``) and uses
  :class:`asyncio.Lock` via :class:`~._core.AsyncCoordinator`.

``httpx.Client`` calls only ``sync_auth_flow``.
``httpx.AsyncClient`` calls only ``async_auth_flow``.
Neither path ever touches the wrong lock type.

Because the two class names differ by one word, each class also *closes* the
flow it does not implement.  Inheriting ``httpx.Auth``'s default there would
let a mismatched pairing sail through with **no ``Authorization`` header at
all** — no exception, no warning, just an unexplained 401.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable, Generator

import httpx

from ._common import CONTROL_EXCEPTIONS, apply_token, guard_refresh, validate_max_retries
from ._core import DEFAULT_ERROR_COOLDOWN, AsyncCoordinator, SyncCoordinator
from ._exceptions import MaxRetriesExceededError, NonReplayableBodyError, RefreshFailedError

try:  # pragma: no cover — import location has been stable since httpx 0.24
    from httpx._multipart import MultipartStream as _MultipartStream

    _MULTIPART: type | None = _MultipartStream
except ImportError:  # pragma: no cover — future httpx reorganisation
    _MULTIPART = None


def _default_is_unauthorized(response: httpx.Response) -> bool:
    return response.status_code == 401


def _explicit_body_conflict() -> RuntimeError:
    return RuntimeError(
        "is_unauthorized inspected the response body, but this auth handler was "
        "constructed with requires_response_body=False. Either drop that argument "
        "(the body is then read on demand) or set it to True."
    )


def _is_replayable_field(field: object) -> bool:
    """Check whether one multipart field can be rendered a second time."""
    file = getattr(field, "file", None)
    if file is None:
        # DataField — an in-memory name/value pair.
        return True
    if isinstance(file, (str, bytes)):
        return True
    headers = getattr(field, "headers", None) or {}
    if headers.get("Content-Transfer-Encoding") == "base64":
        # Older httpx versions skip the rewind for base64 fields.
        return False
    seekable = getattr(file, "seekable", None)
    if seekable is None:
        return False
    return bool(seekable())


def _is_replayable_stream(stream: httpx.SyncByteStream | httpx.AsyncByteStream) -> bool:
    """Check if a request body stream can safely be replayed on retry.

    httpx wraps all bodies into stream objects.  ``bytes``/``str``/``dict``
    bodies become a :class:`httpx.ByteStream` which stores the data in memory
    and can be read multiple times.  Generator and file-like bodies become
    single-use streams that cannot be re-read.

    ``files=`` uploads become a ``MultipartStream``, which httpx **rewinds on
    every iteration** (``FileField.render_data`` seeks back to 0).  Those are
    therefore replayable too, as long as every underlying file is seekable —
    treating them as one-shot would turn a routine token expiry into a hard
    failure for every file upload the caller makes.
    """
    if isinstance(stream, httpx.ByteStream):
        return True
    if _MULTIPART is not None and isinstance(stream, _MULTIPART):
        fields: tuple[object, ...] = tuple(getattr(stream, "fields", ()))
        return all(_is_replayable_field(field) for field in fields)
    return False


def _guard_async_refresh(
    refresh: Callable[[], Awaitable[str]], message: str
) -> Callable[[], Awaitable[str]]:
    """Async counterpart of :func:`._common.guard_refresh`."""

    async def _safe_refresh() -> str:
        try:
            res = await refresh()
            if not res:
                raise ValueError("Async token refresh callable returned empty or None token")
        except CONTROL_EXCEPTIONS:
            raise
        except Exception as exc:
            raise RefreshFailedError(message) from exc
        return res

    return _safe_refresh


class SingleFlightAuth(httpx.Auth):
    """httpx sync auth handler with single-flight token refresh.

    Attach to :class:`httpx.Client` via the ``auth=`` parameter.  When a
    response matches ``is_unauthorized`` the coordinator acquires a lock,
    optionally calls ``refresh()``, updates the ``Authorization`` header, and
    retries the request — all transparently.

    .. warning::

       **This class is sync-only.**  Pairing it with :class:`httpx.AsyncClient`
       raises :class:`TypeError`; use :class:`AsyncSingleFlightAuth` there.

    .. warning::

       **refresh() must use a separate client.**  If ``refresh()`` makes an
       HTTP request through the **same** :class:`httpx.Client` that has this
       auth handler attached, and that request also receives a 401, the
       coordinator will detect the reentrant lock acquisition and raise
       :class:`ReentrantRefreshError` instead of deadlocking.

    .. warning::

       **Generator and non-seekable stream bodies are not retry-safe.**  They
       are consumed on the first send and cannot be replayed after a 401, so
       :class:`NonReplayableBodyError` is raised instead of silently sending
       an empty body.  ``files=`` uploads backed by seekable files *are*
       replayed normally.

    Args:
        get_token:       Returns the current token (no I/O).  A falsy result
                         sends no auth header at all rather than ``"Bearer None"``.
        refresh:         Fetches and persists a new token; returns the new value.
        is_unauthorized: Predicate applied to each response; defaults to
                         ``lambda r: r.status_code == 401``.
        max_retries:     Maximum number of retry attempts after a refresh.
                         Defaults to ``1``.  ``0`` disables refresh entirely:
                         the unauthorized response is returned untouched.
        header_name:     Header the token is written to. Defaults to ``"Authorization"``.
        scheme:          Auth scheme prefix. Defaults to ``"Bearer"``; pass ``""``
                         to send the bare token (e.g. for ``X-API-Key``).
        raise_on_max_retries: When ``False``, the final unauthorized response is
                         returned instead of raising :class:`MaxRetriesExceededError`.
        refresh_wait_timeout: Seconds to wait for *another* caller's in-flight refresh
                         before raising :class:`RefreshTimeoutError`.  ``None``
                         waits forever.  It bounds the waiting only — it cannot
                         abort your ``refresh()`` call, so give that its own
                         I/O timeout too.
        refresh_error_cooldown: Seconds a failed ``refresh()`` is replayed to
                         waiters instead of being retried, so a struggling auth
                         server is not hit once per pending request.
        requires_response_body: Whether httpx buffers the response body before
                         consulting ``is_unauthorized``.  Leave it unset (the
                         default) for automatic behaviour: nothing is buffered
                         until a predicate actually reaches for the body, at
                         which point that one response is read on demand and
                         every later response is buffered up front.  A
                         status-code-only predicate therefore never pays for
                         buffering — which matters for ``client.stream()`` —
                         while a body-inspecting predicate just works.  Pass
                         ``True`` to always buffer, or ``False`` to forbid it
                         (a predicate that then reads the body raises).

    Raises:
        RefreshFailedError:      ``refresh()`` raised an exception.
        RefreshTimeoutError:     ``refresh_wait_timeout`` elapsed while waiting.
        MaxRetriesExceededError: Server still returns an unauthorized response
                                 after ``max_retries`` attempts.
        ReentrantRefreshError:   ``refresh()`` triggered the auth flow on the
                                 same client (would deadlock).
        NonReplayableBodyError:  Request body is a stream/generator that cannot
                                 be replayed for the retry.

    Example::

        auth = SingleFlightAuth(
            get_token=lambda: store["access"],
            refresh=my_refresh,
        )
        with httpx.Client(auth=auth) as client:
            client.get("https://api.example.com/data")
    """

    def __init__(
        self,
        get_token: Callable[[], str],
        refresh: Callable[[], str],
        is_unauthorized: Callable[[httpx.Response], bool] | None = None,
        max_retries: int = 1,
        *,
        header_name: str = "Authorization",
        scheme: str = "Bearer",
        raise_on_max_retries: bool = True,
        refresh_wait_timeout: float | None = None,
        refresh_error_cooldown: float = DEFAULT_ERROR_COOLDOWN,
        requires_response_body: bool | None = None,
    ) -> None:
        validate_max_retries(max_retries)
        self._get_token = get_token
        self._coordinator: SyncCoordinator[str] = SyncCoordinator(
            get_token,
            guard_refresh(refresh, "Token refresh callable raised an exception"),
            error_cooldown=refresh_error_cooldown,
            timeout=refresh_wait_timeout,
        )
        self._is_unauthorized = is_unauthorized or _default_is_unauthorized
        self._max_retries = max_retries
        self._header_name = header_name
        self._scheme = scheme
        self._raise_on_max_retries = raise_on_max_retries
        self._auto_response_body = requires_response_body is None
        self.requires_response_body = bool(requires_response_body)

    def _apply(self, request: httpx.Request, token: str | None) -> None:
        apply_token(request.headers, self._header_name, self._scheme, token)

    def _unauthorized(self, response: httpx.Response) -> bool:
        """Run the predicate, reading the body on demand if it needs one.

        Two consequences of this lazy probe are worth knowing — both avoidable
        by passing ``requires_response_body=True`` explicitly:

        * The predicate runs **twice** on the one response that first reaches
          for the body (once until it raises, once on the buffered response).
          Every later response costs exactly one call.  A predicate with side
          effects — a metrics counter, a log line — therefore double-fires once
          per auth instance.
        * Detection relies on ``httpx.ResponseNotRead`` propagating out of the
          predicate.  A predicate that catches its own exceptions swallows the
          signal, so it silently sees an empty body and reports "authorized".
        """
        try:
            return self._is_unauthorized(response)
        except httpx.ResponseNotRead:
            if not self._auto_response_body:
                raise _explicit_body_conflict() from None
            # The predicate inspects the body. Read it now, and buffer up front
            # from here on — so a status-code-only predicate never pays for it.
            response.read()
            self.requires_response_body = True
            return self._is_unauthorized(response)

    def sync_auth_flow(
        self, request: httpx.Request
    ) -> Generator[httpx.Request, httpx.Response, None]:
        """Sync auth flow: inject token, handle 401 with single-flight refresh."""
        generation = self._coordinator.token_generation()
        token = self._get_token()
        self._apply(request, token)
        response = yield request
        # httpx only honours requires_response_body inside the *base*
        # sync_auth_flow, which this override replaces — so do it here.
        if self.requires_response_body:
            response.read()

        if self._max_retries == 0:
            # Refresh disabled — hand the response back exactly as received.
            return

        retries = 0
        # Evaluated once per response — a user predicate may be expensive, and
        # calling it twice on the same response was never intended.
        unauthorized = self._unauthorized(response)
        while unauthorized and retries < self._max_retries:
            if not _is_replayable_stream(request.stream):
                raise NonReplayableBodyError(
                    "Cannot retry a request with a non-replayable body "
                    "(generator, or non-seekable file/stream). Buffer the body into "
                    "bytes before sending, or handle token refresh at a higher layer. "
                    "See: https://github.com/alibeg-begow/singleflight_auth#limitations"
                )
            retries += 1
            token, generation = self._coordinator.resolve(generation, stale_token=token)
            self._apply(request, token)
            response = yield request
            if self.requires_response_body:
                response.read()
            unauthorized = self._unauthorized(response)

        if unauthorized:
            if not self._raise_on_max_retries:
                return
            # httpx closes the response when the flow raises, so buffer it
            # first — otherwise exc.response is unreadable to the caller.
            response.read()
            raise MaxRetriesExceededError(
                f"Still unauthorized after {self._max_retries} retry attempt(s)",
                response=response,
            )

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        """Reject async usage instead of silently sending an unauthenticated request."""
        raise TypeError(
            "SingleFlightAuth is sync-only and cannot be used with httpx.AsyncClient. "
            "Use AsyncSingleFlightAuth (with an async refresh callable) instead."
        )
        yield request  # pragma: no cover — unreachable; makes this an async generator


class AsyncSingleFlightAuth(httpx.Auth):
    """httpx async auth handler with single-flight token refresh.

    Attach to :class:`httpx.AsyncClient` via the ``auth=`` parameter.
    The internal lock is :class:`asyncio.Lock` — it never blocks the event
    loop while waiting for an in-flight refresh, and a separate lock is kept
    per event loop so one shared instance survives repeated
    :func:`asyncio.run` calls, ``pytest-asyncio`` tests and thread-per-loop
    servers.

    .. warning::

       **This class is async-only.**  Pairing it with :class:`httpx.Client`
       raises :class:`TypeError`; use :class:`SingleFlightAuth` there.

    .. warning::

       **refresh() must use a separate client.**  See :class:`SingleFlightAuth`
       for details on the reentrant lock protection.

    .. warning::

       **Generator and non-seekable stream bodies are not retry-safe.**  See
       :class:`SingleFlightAuth` for details.

    Args:
        get_token:       Returns the current token (sync, no I/O).
        refresh:         Async callable; fetches and persists a new token,
                         returns the new value.
        is_unauthorized: Predicate applied to each response.
        max_retries:     Maximum retry attempts. Defaults to ``1``; ``0``
                         disables refresh entirely.
        header_name:     Header the token is written to.
        scheme:          Auth scheme prefix; ``""`` sends the bare token.
        raise_on_max_retries: When ``False``, return the final unauthorized
                         response instead of raising.
        refresh_wait_timeout: Seconds to wait for *another* caller's in-flight
                         refresh; it cannot abort your own ``refresh()``.
        refresh_error_cooldown: Seconds a failed ``refresh()`` is replayed to
                         waiters instead of being retried.
        requires_response_body: See :class:`SingleFlightAuth`; the body is read
                         on demand the first time a predicate needs it.

    Raises:
        RefreshFailedError:      ``refresh()`` raised an exception.
        RefreshTimeoutError:     ``refresh_wait_timeout`` elapsed while waiting.
        MaxRetriesExceededError: Still unauthorized after ``max_retries``.
        ReentrantRefreshError:   ``refresh()`` triggered the auth flow on the
                                 same async client.
        NonReplayableBodyError:  Request body cannot be replayed.

    Example::

        auth = AsyncSingleFlightAuth(
            get_token=lambda: store["access"],
            refresh=my_async_refresh,
        )
        async with httpx.AsyncClient(auth=auth) as client:
            await client.get("https://api.example.com/data")
    """

    def __init__(
        self,
        get_token: Callable[[], str],
        refresh: Callable[[], Awaitable[str]],
        is_unauthorized: Callable[[httpx.Response], bool] | None = None,
        max_retries: int = 1,
        *,
        header_name: str = "Authorization",
        scheme: str = "Bearer",
        raise_on_max_retries: bool = True,
        refresh_wait_timeout: float | None = None,
        refresh_error_cooldown: float = DEFAULT_ERROR_COOLDOWN,
        requires_response_body: bool | None = None,
    ) -> None:
        validate_max_retries(max_retries)
        self._get_token = get_token
        self._coordinator: AsyncCoordinator[str] = AsyncCoordinator(
            get_token,
            _guard_async_refresh(refresh, "Async token refresh callable raised an exception"),
            error_cooldown=refresh_error_cooldown,
            timeout=refresh_wait_timeout,
        )
        self._is_unauthorized = is_unauthorized or _default_is_unauthorized
        self._max_retries = max_retries
        self._header_name = header_name
        self._scheme = scheme
        self._raise_on_max_retries = raise_on_max_retries
        self._auto_response_body = requires_response_body is None
        self.requires_response_body = bool(requires_response_body)

    def _apply(self, request: httpx.Request, token: str | None) -> None:
        apply_token(request.headers, self._header_name, self._scheme, token)

    async def _unauthorized(self, response: httpx.Response) -> bool:
        """Run the predicate, reading the body on demand if it needs one.

        Async counterpart of :meth:`SingleFlightAuth._unauthorized`; see there
        for the two caveats of the lazy probe.  Note ``aread()`` rather than
        ``read()`` — the sync call cannot iterate an async stream.
        """
        try:
            return self._is_unauthorized(response)
        except httpx.ResponseNotRead:
            if not self._auto_response_body:
                raise _explicit_body_conflict() from None
            await response.aread()
            self.requires_response_body = True
            return self._is_unauthorized(response)

    def sync_auth_flow(
        self, request: httpx.Request
    ) -> Generator[httpx.Request, httpx.Response, None]:
        """Reject sync usage instead of silently sending an unauthenticated request."""
        raise TypeError(
            "AsyncSingleFlightAuth is async-only and cannot be used with httpx.Client. "
            "Use SingleFlightAuth (with a sync refresh callable) instead."
        )
        yield request  # pragma: no cover — unreachable; makes this a generator

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        """Async auth flow: inject token, handle 401 with single-flight refresh."""
        generation = self._coordinator.token_generation()
        token = self._get_token()
        self._apply(request, token)
        response = yield request
        # httpx only honours requires_response_body inside the *base*
        # async_auth_flow, which this override replaces — so do it here.
        if self.requires_response_body:
            await response.aread()

        if self._max_retries == 0:
            # Refresh disabled — hand the response back exactly as received.
            return

        retries = 0
        # Evaluated once per response — a user predicate may be expensive, and
        # calling it twice on the same response was never intended.
        unauthorized = await self._unauthorized(response)
        while unauthorized and retries < self._max_retries:
            if not _is_replayable_stream(request.stream):
                raise NonReplayableBodyError(
                    "Cannot retry a request with a non-replayable body "
                    "(generator, or non-seekable file/stream). Buffer the body into "
                    "bytes before sending, or handle token refresh at a higher layer. "
                    "See: https://github.com/alibeg-begow/singleflight_auth#limitations"
                )
            retries += 1
            token, generation = await self._coordinator.resolve(generation, stale_token=token)
            self._apply(request, token)
            response = yield request
            if self.requires_response_body:
                await response.aread()
            unauthorized = await self._unauthorized(response)

        if unauthorized:
            if not self._raise_on_max_retries:
                return
            # httpx closes the response when the flow raises, so buffer it
            # first — otherwise exc.response is unreadable to the caller.
            await response.aread()
            raise MaxRetriesExceededError(
                f"Still unauthorized after {self._max_retries} retry attempt(s)",
                response=response,
            )
