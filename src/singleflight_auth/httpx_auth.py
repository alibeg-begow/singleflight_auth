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
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable, Generator

import httpx

from ._core import AsyncCoordinator, SyncCoordinator
from ._exceptions import MaxRetriesExceededError, NonReplayableBodyError, RefreshFailedError


def _is_replayable_stream(stream: httpx.SyncByteStream | httpx.AsyncByteStream) -> bool:
    """Check if a request body stream can safely be replayed on retry.

    httpx wraps all bodies into stream objects.  ``bytes``/``str``/``dict``
    bodies become a :class:`httpx.ByteStream` which stores the data in memory
    and can be read multiple times.  Generator and file-like bodies become
    single-use streams that cannot be re-read.

    We check for :class:`httpx.ByteStream` specifically — it is the only
    built-in stream type that is guaranteed safe for replay.
    """
    # ByteStream wraps in-memory bytes — always safe to replay
    return isinstance(stream, httpx.ByteStream)


class SingleFlightAuth(httpx.Auth):
    """httpx sync auth handler with single-flight token refresh.

    Attach to :class:`httpx.Client` via the ``auth=`` parameter.  When a
    response matches ``is_unauthorized`` the coordinator acquires a lock,
    optionally calls ``refresh()``, updates the ``Authorization`` header, and
    retries the request — all transparently.

    .. warning::

       **refresh() must use a separate client.**  If ``refresh()`` makes an
       HTTP request through the **same** :class:`httpx.Client` that has this
       auth handler attached, and that request also receives a 401, the
       coordinator will detect the reentrant lock acquisition and raise
       :class:`ReentrantRefreshError` instead of deadlocking.

    .. warning::

       **Stream/generator/file bodies are not retry-safe.**  If the request
       body is a generator, file, or other one-shot stream, it cannot be
       replayed after a 401.  In this case :class:`NonReplayableBodyError`
       is raised instead of silently sending an empty body.

    Args:
        get_token:       Returns the current bearer token (no I/O).
        refresh:         Fetches and persists a new token; returns the new value.
        is_unauthorized: Predicate applied to each response; defaults to
                         ``lambda r: r.status_code == 401``.
        max_retries:     Maximum number of retry attempts after a refresh.
                         Defaults to ``1``.

    Raises:
        RefreshFailedError:      ``refresh()`` raised an exception.
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
        is_unauthorized: Callable[[httpx.Response], bool] = lambda r: r.status_code == 401,
        max_retries: int = 1,
    ) -> None:
        self._get_token = get_token
        self._coordinator = SyncCoordinator(get_token, self._wrap_refresh(refresh))
        self._is_unauthorized = is_unauthorized
        self._max_retries = max_retries

    @staticmethod
    def _wrap_refresh(refresh: Callable[[], str]) -> Callable[[], str]:
        """Wrap ``refresh`` so any exception becomes :class:`RefreshFailedError`."""

        def _safe_refresh() -> str:
            try:
                res = refresh()
                if not res:
                    raise ValueError("Token refresh callable returned empty or None token")
                return res
            except Exception as exc:  # noqa: BLE001 — intentional broad catch; we re-raise
                raise RefreshFailedError(
                    "Token refresh callable raised an exception"
                ) from exc

        return _safe_refresh

    def sync_auth_flow(
        self, request: httpx.Request
    ) -> Generator[httpx.Request, httpx.Response, None]:
        """Sync auth flow: inject token, handle 401 with single-flight refresh."""
        token = self._get_token()
        request.headers["Authorization"] = f"Bearer {token}"
        response = yield request

        retries = 0
        while self._is_unauthorized(response) and retries < self._max_retries:
            if not _is_replayable_stream(request.stream):
                raise NonReplayableBodyError(
                    "Cannot retry a request with a non-replayable body "
                    "(generator, file, or stream). Buffer the body into bytes "
                    "before sending, or handle token refresh at a higher layer. "
                    "See: https://github.com/alibeg-begow/singleflight_auth#limitations"
                )
            retries += 1
            new_token = self._coordinator.resolve(stale_token=token)
            token = new_token
            request.headers["Authorization"] = f"Bearer {new_token}"
            response = yield request

        if self._is_unauthorized(response) and retries >= self._max_retries:
            raise MaxRetriesExceededError(
                f"Still unauthorized after {self._max_retries} retry attempt(s)"
            )


class AsyncSingleFlightAuth(httpx.Auth):
    """httpx async auth handler with single-flight token refresh.

    Attach to :class:`httpx.AsyncClient` via the ``auth=`` parameter.
    The internal lock is :class:`asyncio.Lock` — it never blocks the event
    loop while waiting for an in-flight refresh.

    .. warning::

       **refresh() must use a separate client.**  See :class:`SingleFlightAuth`
       for details on the reentrant lock protection.

    .. warning::

       **Stream/generator/file bodies are not retry-safe.**  See
       :class:`SingleFlightAuth` for details.

    Args:
        get_token:       Returns the current bearer token (sync, no I/O).
        refresh:         Async callable; fetches and persists a new token,
                         returns the new value.
        is_unauthorized: Predicate applied to each response.
        max_retries:     Maximum retry attempts. Defaults to ``1``.

    Raises:
        RefreshFailedError:      ``refresh()`` raised an exception.
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
        is_unauthorized: Callable[[httpx.Response], bool] = lambda r: r.status_code == 401,
        max_retries: int = 1,
    ) -> None:
        self._get_token = get_token
        self._coordinator = AsyncCoordinator(get_token, self._wrap_refresh(refresh))
        self._is_unauthorized = is_unauthorized
        self._max_retries = max_retries

    @staticmethod
    def _wrap_refresh(
        refresh: Callable[[], Awaitable[str]],
    ) -> Callable[[], Awaitable[str]]:
        """Wrap async ``refresh`` so any exception becomes :class:`RefreshFailedError`."""

        async def _safe_refresh() -> str:
            try:
                res = await refresh()
                if not res:
                    raise ValueError("Async token refresh callable returned empty or None token")
                return res
            except Exception as exc:  # noqa: BLE001
                raise RefreshFailedError(
                    "Async token refresh callable raised an exception"
                ) from exc

        return _safe_refresh

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        """Async auth flow: inject token, handle 401 with single-flight refresh."""
        token = self._get_token()
        request.headers["Authorization"] = f"Bearer {token}"
        response = yield request

        retries = 0
        while self._is_unauthorized(response) and retries < self._max_retries:
            if not _is_replayable_stream(request.stream):
                raise NonReplayableBodyError(
                    "Cannot retry a request with a non-replayable body "
                    "(generator, file, or stream). Buffer the body into bytes "
                    "before sending, or handle token refresh at a higher layer. "
                    "See: https://github.com/alibeg-begow/singleflight_auth#limitations"
                )
            retries += 1
            new_token = await self._coordinator.resolve(stale_token=token)
            token = new_token
            request.headers["Authorization"] = f"Bearer {new_token}"
            response = yield request

        if self._is_unauthorized(response) and retries >= self._max_retries:
            raise MaxRetriesExceededError(
                f"Still unauthorized after {self._max_retries} retry attempt(s)"
            )
