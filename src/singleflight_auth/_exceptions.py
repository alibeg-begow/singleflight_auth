"""Custom exceptions for singleflight-auth."""

from __future__ import annotations

from typing import Any


class RefreshFailedError(Exception):
    """Raised when the user-supplied refresh() callable raises an exception.

    The original exception is always chained via ``__cause__`` so that
    callers can inspect the root cause for logging or telemetry.

    While the auth server is down, the *same* failure is replayed to every
    waiter for a short cooldown window instead of each of them hammering the
    refresh endpoint again — see ``refresh_error_cooldown``.

    Example::

        try:
            client.get("/protected")
        except RefreshFailedError as exc:
            logger.error("Token refresh failed: %s", exc.__cause__)
            redirect_to_login()
    """


class RefreshTimeoutError(RefreshFailedError):
    """Raised when *waiting* for another caller's refresh exceeds ``refresh_wait_timeout``.

    Without it, a hung ``refresh()`` (e.g. an HTTP call with no timeout of its
    own) parks every other thread or coroutine forever.  Pass
    ``refresh_wait_timeout=<seconds>`` to bound that wait.

    .. important::

       The name says *wait* on purpose: this bounds how long you queue behind
       somebody else, **not** how long your own ``refresh()`` may run.  A
       blocking callable cannot be interrupted from the outside, and killing it
       mid-flight could leave the caller's token store half-written — so the
       winner is always allowed to finish.

       The consequence is worth knowing: if the winner hangs *forever*, it holds
       the lock forever, and every later request fails with this exception
       rather than deadlocking silently.  That is a deliberate, visible
       degradation — but it is only a symptom.  **Always give ``refresh()`` its
       own I/O timeout**; ``refresh_wait_timeout`` is the safety net, not the fix.

    Subclasses :class:`RefreshFailedError`, so existing handlers keep working.

    Example::

        def refresh() -> str:
            # the real timeout lives here
            resp = httpx.post("https://auth.example.com/token", timeout=10.0)
            return resp.json()["access_token"]

        auth = SingleFlightAuth(
            get_token=..., refresh=refresh, refresh_wait_timeout=15.0
        )
    """


class MaxRetriesExceededError(Exception):
    """Raised when the request is still unauthorized after *max_retries* attempts.

    This typically means the refresh succeeded (a new token was obtained)
    but the server still returns 401 — which can indicate the new token is
    also invalid, or that the endpoint requires additional permissions.

    The final response is attached as :attr:`response` so callers can inspect
    the status code, the ``WWW-Authenticate`` / ``Retry-After`` headers, and
    the error body.  Pass ``raise_on_max_retries=False`` to receive that
    response as a normal return value instead of an exception.

    Attributes:
        response: The last unauthorized response (``httpx.Response`` or
            ``requests.Response``), or ``None`` if unavailable.

    Example::

        try:
            client.get("/admin-only")
        except MaxRetriesExceededError as exc:
            logger.warning("Still %s after refresh", exc.response.status_code)
    """

    def __init__(self, message: str, response: Any = None) -> None:
        super().__init__(message)
        self.response = response


class ReentrantRefreshError(RuntimeError):
    """Raised when refresh() triggers the auth flow on the same client/session.

    This happens when the user-supplied ``refresh()`` callable makes an HTTP
    request through the **same** client or session that has this auth handler
    attached, and that request also receives a 401 response — causing the
    coordinator to re-enter its own lock from the same thread or coroutine.

    ``threading.Lock`` and ``asyncio.Lock`` are **not reentrant**, so this
    would cause a permanent deadlock.  Instead of silently hanging, this
    exception is raised immediately with a clear error message.

    It is never wrapped in :class:`RefreshFailedError`, so
    ``except ReentrantRefreshError`` always works.

    **Fix:** Use a **separate**, unauthenticated client for the refresh call::

        # Correct — uses a fresh httpx.post() without auth
        def refresh() -> str:
            resp = httpx.post("https://auth.example.com/token", ...)
            return resp.json()["access_token"]

        # Wrong — reuses the client that has SingleFlightAuth attached
        def refresh() -> str:
            resp = client.post("https://auth.example.com/token", ...)
            return resp.json()["access_token"]
    """


class NonReplayableBodyError(RuntimeError):
    """Raised when a 401 retry cannot be performed because the request body is not replayable.

    Stream, generator, and non-seekable file-like request bodies are consumed
    on the first send and cannot be re-read for a retry.  Rather than silently
    sending an empty body (causing data loss) or raising a cryptic low-level
    error, this exception is raised with a clear explanation.

    Multipart uploads (``files=``) backed by **seekable** files *are*
    replayable — httpx rewinds them on every iteration — so they are retried
    normally and never raise this.

    **Fix:** Either buffer the body into ``bytes`` before sending, or handle
    the 401/refresh logic at a higher layer that can re-construct the body::

        # Correct — buffer into bytes first
        data = my_stream.read()
        client.post("/upload", content=data)

        # Risky — generator body cannot be replayed on 401
        client.post("/upload", content=my_generator())
    """
