"""
singleflight-auth — single-flight token refresh for httpx and requests.

Public API surface:
    SingleFlightAuth          — httpx.Client (sync) auth handler
    AsyncSingleFlightAuth     — httpx.AsyncClient (async) auth handler
    RequestsSingleFlightAuth  — requests.Session auth handler
    RefreshFailedError        — raised when the user-supplied refresh() raises
    RefreshTimeoutError       — raised when refresh_wait_timeout elapses
    MaxRetriesExceededError   — raised when max_retries is exhausted
    ReentrantRefreshError     — raised when refresh() re-enters the same lock
    NonReplayableBodyError    — raised when a stream body cannot be retried

``httpx`` and ``requests`` are **optional** extras, so the integration classes
are imported lazily (:pep:`562`).  Importing this package with only one of
them installed works; touching the class that needs the missing one raises an
``ImportError`` naming the extra to install.

Important usage notes:
    - Create the auth instance **once** and share it across all requests.
      Each instance has its own lock, so multiple instances defeat the
      single-flight guarantee.
    - The ``refresh()`` callable must use a **separate** client/session
      (without this auth handler attached) to avoid deadlocks.
    - Generator and non-seekable stream request bodies cannot be replayed on
      401 retry.  Buffer them into ``bytes`` before sending.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

from ._exceptions import (
    MaxRetriesExceededError,
    NonReplayableBodyError,
    ReentrantRefreshError,
    RefreshFailedError,
    RefreshTimeoutError,
)

if TYPE_CHECKING:  # pragma: no cover — import-time types only
    from .httpx_auth import AsyncSingleFlightAuth as AsyncSingleFlightAuth
    from .httpx_auth import SingleFlightAuth as SingleFlightAuth
    from .requests_auth import SingleFlightAuth as RequestsSingleFlightAuth

__all__ = [
    "SingleFlightAuth",
    "AsyncSingleFlightAuth",
    "RequestsSingleFlightAuth",
    "RefreshFailedError",
    "RefreshTimeoutError",
    "MaxRetriesExceededError",
    "ReentrantRefreshError",
    "NonReplayableBodyError",
]

__version__ = "0.2.0"

#: attribute -> (submodule, attribute in submodule, extra that provides it)
_LAZY_ATTRS: dict[str, tuple[str, str, str]] = {
    "SingleFlightAuth": (".httpx_auth", "SingleFlightAuth", "httpx"),
    "AsyncSingleFlightAuth": (".httpx_auth", "AsyncSingleFlightAuth", "httpx"),
    "RequestsSingleFlightAuth": (".requests_auth", "SingleFlightAuth", "requests"),
}


def __getattr__(name: str) -> Any:
    """Import an integration class on first access (:pep:`562`)."""
    try:
        module_name, attr, extra = _LAZY_ATTRS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None

    try:
        obj = getattr(import_module(module_name, __package__), attr)
    except ImportError as exc:
        raise ImportError(
            f"{name} requires the {extra!r} extra, which is not installed. "
            f"Install it with: pip install singleflight-auth[{extra}]"
        ) from exc

    globals()[name] = obj  # cache so __getattr__ runs at most once per name
    return obj


def __dir__() -> list[str]:
    return sorted(__all__)
