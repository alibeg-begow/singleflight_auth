"""
requests integration for singleflight-auth.

Provides :class:`SingleFlightAuth` for :class:`requests.Session`.

``requests`` is a synchronous library with no interceptor concept.  The
idiomatic approach is to use a **response hook**: the auth object registers
``_on_response`` which is called by requests after every response.  When a
401 is detected the hook consumes the response body (to release the connection
back to the pool), re-builds the request with a fresh token, and re-sends it
via ``response.connection.send()``.

Retry state (generation observed, token sent, retry count) is stored directly
on the :class:`requests.PreparedRequest` object as private attributes
(``_sf_generation``, ``_sf_token_used``, ``_sf_retry_count``).  This keeps the state
per-request so concurrent threads never interfere with each other's counters.
"""

from __future__ import annotations

import io
from collections.abc import Callable

import requests
import requests.auth

from ._common import apply_token, guard_refresh, validate_max_retries
from ._core import DEFAULT_ERROR_COOLDOWN, SyncCoordinator
from ._exceptions import MaxRetriesExceededError, NonReplayableBodyError


def _default_is_unauthorized(response: requests.Response) -> bool:
    return response.status_code == 401


def _is_replayable_body(body: object) -> bool:
    """Check if a requests body can safely be replayed on retry.

    ``bytes``, ``str``, ``memoryview`` and ``None`` bodies are safe because
    they are fully in-memory and deterministic — this covers ``data=``,
    ``json=`` and ``files=``, all of which ``PreparedRequest`` has already
    rendered to ``bytes`` by the time the hook runs.  File-like objects,
    generators, and iterators are single-use: ``PreparedRequest.copy()``
    copies the reference without rewinding it, so replaying would send a body
    that is already exhausted.
    """
    if body is None:
        return True
    if isinstance(body, (bytes, str, memoryview)):
        return True
    # BytesIO can be seeked back to 0 — but we'd need to do it ourselves,
    # and PreparedRequest.copy() doesn't seek.  Treat as non-replayable
    # to be safe.
    if isinstance(body, io.BytesIO):
        return False
    return False


class SingleFlightAuth(requests.auth.AuthBase):
    """requests auth handler with single-flight token refresh.

    Attach to :class:`requests.Session` via the ``auth=`` parameter or pass
    directly to a single request.  When a response matches
    ``is_unauthorized`` the coordinator acquires a lock, optionally calls
    ``refresh()``, and re-sends the request with the new token.

    .. warning::

       **refresh() must use a separate session.**  If ``refresh()`` makes
       an HTTP request through the **same** session that has this auth
       handler attached, and that request also receives a 401, the
       coordinator will detect the reentrant lock acquisition and raise
       :class:`ReentrantRefreshError` instead of deadlocking.

    .. warning::

       **Stream/generator/file bodies are not retry-safe.**  If the request
       body is a generator, file, or other one-shot stream, it cannot be
       replayed after a 401.  In this case :class:`NonReplayableBodyError`
       is raised instead of silently sending an empty body or hanging.

    Args:
        get_token:       Returns the current token (no I/O).  A falsy result
                         sends no auth header at all rather than ``"Bearer None"``.
        refresh:         Fetches and persists a new token; returns the new value.
        is_unauthorized: Predicate applied to each response; defaults to
                         ``lambda r: r.status_code == 401``.
        max_retries:     Maximum retry attempts. Defaults to ``1``.  ``0``
                         disables refresh entirely: the unauthorized response
                         is returned untouched.
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
                         waiters instead of being retried.

    Raises:
        RefreshFailedError:      ``refresh()`` raised an exception.
        RefreshTimeoutError:     ``refresh_wait_timeout`` elapsed while waiting.
        MaxRetriesExceededError: Still unauthorized after ``max_retries``.
        ReentrantRefreshError:   ``refresh()`` triggered the auth flow on the
                                 same session.
        NonReplayableBodyError:  Request body cannot be replayed for retry.

    Example::

        auth = SingleFlightAuth(
            get_token=lambda: store["access"],
            refresh=my_refresh,
        )
        session = requests.Session()
        session.auth = auth
        session.get("https://api.example.com/data")
    """

    def __init__(
        self,
        get_token: Callable[[], str],
        refresh: Callable[[], str],
        is_unauthorized: Callable[[requests.Response], bool] | None = None,
        max_retries: int = 1,
        *,
        header_name: str = "Authorization",
        scheme: str = "Bearer",
        raise_on_max_retries: bool = True,
        refresh_wait_timeout: float | None = None,
        refresh_error_cooldown: float = DEFAULT_ERROR_COOLDOWN,
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

    def _apply(self, request: requests.PreparedRequest, token: str | None) -> None:
        apply_token(request.headers, self._header_name, self._scheme, token)

    def __call__(self, request: requests.PreparedRequest) -> requests.PreparedRequest:
        """Inject the current token and register the response hook."""
        # Read the generation *before* the token so a refresh landing between
        # the two can only make us look stale, never look fresh.
        request._sf_generation = self._coordinator.token_generation()  # type: ignore[attr-defined]
        token = self._get_token()
        self._apply(request, token)
        # Store the state on the request object so concurrent threads each
        # track their own independently.
        request._sf_token_used = token  # type: ignore[attr-defined]
        request._sf_retry_count = 0  # type: ignore[attr-defined]
        request.register_hook("response", self._on_response)  # type: ignore[no-untyped-call]
        return request

    def _on_response(
        self, response: requests.Response, **kwargs: object
    ) -> requests.Response:
        """Response hook: handle 401 with a single-flight token refresh."""
        if self._max_retries == 0 or not self._is_unauthorized(response):
            return response

        retried: int = getattr(response.request, "_sf_retry_count", 0)
        if retried >= self._max_retries:
            response.content  # noqa: B018 — intentional side-effect to release connection
            if not self._raise_on_max_retries:
                return response
            raise MaxRetriesExceededError(
                f"Still unauthorized after {self._max_retries} retry attempt(s)",
                response=response,
            )

        if not _is_replayable_body(response.request.body):
            response.content  # noqa: B018 — release connection
            raise NonReplayableBodyError(
                "Cannot retry a request with a non-replayable body "
                "(generator, file, or stream). Buffer the body into bytes "
                "before sending, or handle token refresh at a higher layer. "
                "See: https://github.com/alibeg-begow/singleflight_auth#limitations"
            )

        # Consume the response body to release the connection back to the pool
        # *before* resolve(), which may block for the whole duration of the
        # refresh.  requests dispatches response hooks before it reads the
        # body itself, so refreshing first would keep every 401 connection
        # checked out of the pool for as long as the refresh takes.
        response.content  # noqa: B018 — intentional side-effect

        seen_generation: int = getattr(response.request, "_sf_generation", 0)
        stale_token: str = getattr(response.request, "_sf_token_used", "")
        new_token, generation = self._coordinator.resolve(seen_generation, stale_token=stale_token)

        new_request = response.request.copy()
        self._apply(new_request, new_token)
        new_request._sf_generation = generation  # type: ignore[attr-defined]
        new_request._sf_token_used = new_token  # type: ignore[attr-defined]
        new_request._sf_retry_count = retried + 1  # type: ignore[attr-defined]

        new_response = response.connection.send(new_request, **kwargs)  # type: ignore[arg-type]
        new_response.history = [*response.history, response]
        return self._on_response(new_response, **kwargs)
