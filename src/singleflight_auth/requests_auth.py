"""
requests integration for singleflight-auth.

Provides :class:`SingleFlightAuth` for :class:`requests.Session`.

``requests`` is a synchronous library with no interceptor concept.  The
idiomatic approach is to use a **response hook**: the auth object registers
``_on_response`` which is called by requests after every response.  When a
401 is detected the hook consumes the response body (to release the connection
back to the pool), re-builds the request with a fresh token, and re-sends it
via ``response.connection.send()``.

Retry state (token used, retry count) is stored directly on the
:class:`requests.PreparedRequest` object as private attributes
(``_sf_token_used``, ``_sf_retry_count``).  This keeps the state
per-request so concurrent threads never interfere with each other's counters.
"""

from __future__ import annotations

from typing import Callable

import requests
import requests.auth

from ._core import SyncCoordinator
from ._exceptions import MaxRetriesExceededError, RefreshFailedError


class SingleFlightAuth(requests.auth.AuthBase):
    """requests auth handler with single-flight token refresh.

    Attach to :class:`requests.Session` via the ``auth=`` parameter or pass
    directly to a single request.  When a response matches
    ``is_unauthorized`` the coordinator acquires a lock, optionally calls
    ``refresh()``, and re-sends the request with the new token.

    Args:
        get_token:       Returns the current bearer token (no I/O).
        refresh:         Fetches and persists a new token; returns the new value.
        is_unauthorized: Predicate applied to each response; defaults to
                         ``lambda r: r.status_code == 401``.
        max_retries:     Maximum retry attempts. Defaults to ``1``.

    Raises:
        RefreshFailedError:      ``refresh()`` raised an exception.
        MaxRetriesExceededError: Still unauthorized after ``max_retries``.

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
        is_unauthorized: Callable[[requests.Response], bool] = lambda r: r.status_code == 401,
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
            except Exception as exc:  # noqa: BLE001
                raise RefreshFailedError(
                    "Token refresh callable raised an exception"
                ) from exc

        return _safe_refresh

    def __call__(self, request: requests.PreparedRequest) -> requests.PreparedRequest:
        """Inject the current token and register the response hook."""
        token = self._get_token()
        request.headers["Authorization"] = f"Bearer {token}"
        # Store the token used and retry count on the request object so
        # concurrent threads each track their own state independently.
        request._sf_token_used = token  # type: ignore[attr-defined]
        request._sf_retry_count = 0  # type: ignore[attr-defined]
        request.register_hook("response", self._on_response)  # type: ignore[no-untyped-call]
        return request

    def _on_response(
        self, response: requests.Response, **kwargs: object
    ) -> requests.Response:
        """Response hook: handle 401 with a single-flight token refresh."""
        if not self._is_unauthorized(response):
            return response

        retried: int = getattr(response.request, "_sf_retry_count", 0)
        if retried >= self._max_retries:
            raise MaxRetriesExceededError(
                f"Still unauthorized after {self._max_retries} retry attempt(s)"
            )

        stale_token: str = getattr(response.request, "_sf_token_used", "")
        new_token = self._coordinator.resolve(stale_token=stale_token)

        # Consume the response body to release the connection back to the pool.
        response.content  # noqa: B018 — intentional side-effect

        new_request = response.request.copy()
        new_request.headers["Authorization"] = f"Bearer {new_token}"
        new_request._sf_token_used = new_token  # type: ignore[attr-defined]
        new_request._sf_retry_count = retried + 1  # type: ignore[attr-defined]

        new_response = response.connection.send(new_request, **kwargs)  # type: ignore[arg-type]
        new_response.history = [*response.history, response]
        return self._on_response(new_response, **kwargs)
