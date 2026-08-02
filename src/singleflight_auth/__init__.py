"""
singleflight-auth — single-flight token refresh for httpx and requests.

Public API surface:
    SingleFlightAuth          — httpx.Client (sync) auth handler
    AsyncSingleFlightAuth     — httpx.AsyncClient (async) auth handler
    RequestsSingleFlightAuth  — requests.Session auth handler
    RefreshFailedError        — raised when the user-supplied refresh() raises
    MaxRetriesExceededError   — raised when max_retries is exhausted
"""

from ._exceptions import MaxRetriesExceededError, RefreshFailedError
from .httpx_auth import AsyncSingleFlightAuth, SingleFlightAuth
from .requests_auth import SingleFlightAuth as RequestsSingleFlightAuth

__all__ = [
    "SingleFlightAuth",
    "AsyncSingleFlightAuth",
    "RequestsSingleFlightAuth",
    "RefreshFailedError",
    "MaxRetriesExceededError",
]

__version__ = "0.1.2"
