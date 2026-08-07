"""
singleflight-auth — single-flight token refresh for httpx and requests.

Public API surface:
    SingleFlightAuth          — httpx.Client (sync) auth handler
    AsyncSingleFlightAuth     — httpx.AsyncClient (async) auth handler
    RequestsSingleFlightAuth  — requests.Session auth handler
    RefreshFailedError        — raised when the user-supplied refresh() raises
    MaxRetriesExceededError   — raised when max_retries is exhausted
    ReentrantRefreshError     — raised when refresh() re-enters the same lock
    NonReplayableBodyError    — raised when a stream body cannot be retried

Important usage notes:
    - Create the auth instance **once** and share it across all requests.
      Each instance has its own lock, so multiple instances defeat the
      single-flight guarantee.
    - The ``refresh()`` callable must use a **separate** client/session
      (without this auth handler attached) to avoid deadlocks.
    - Stream/generator/file request bodies cannot be replayed on 401 retry.
      Buffer them into ``bytes`` before sending.
"""

from ._exceptions import (
    MaxRetriesExceededError,
    NonReplayableBodyError,
    ReentrantRefreshError,
    RefreshFailedError,
)
from .httpx_auth import AsyncSingleFlightAuth, SingleFlightAuth
from .requests_auth import SingleFlightAuth as RequestsSingleFlightAuth

__all__ = [
    "SingleFlightAuth",
    "AsyncSingleFlightAuth",
    "RequestsSingleFlightAuth",
    "RefreshFailedError",
    "MaxRetriesExceededError",
    "ReentrantRefreshError",
    "NonReplayableBodyError",
]

__version__ = "0.1.3"
