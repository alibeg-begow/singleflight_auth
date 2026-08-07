"""Custom exceptions for singleflight-auth."""


class RefreshFailedError(Exception):
    """Raised when the user-supplied refresh() callable raises an exception.

    The original exception is always chained via ``__cause__`` so that
    callers can inspect the root cause for logging or telemetry.

    Example::

        try:
            client.get("/protected")
        except RefreshFailedError as exc:
            logger.error("Token refresh failed: %s", exc.__cause__)
            redirect_to_login()
    """


class MaxRetriesExceededError(Exception):
    """Raised when the request is still unauthorized after *max_retries* attempts.

    This typically means the refresh succeeded (a new token was obtained)
    but the server still returns 401 — which can indicate the new token is
    also invalid, or that the endpoint requires additional permissions.

    Example::

        try:
            client.get("/admin-only")
        except MaxRetriesExceededError:
            logger.warning("Still 401 after refresh — user lacks permission")
    """


class ReentrantRefreshError(RuntimeError):
    """Raised when refresh() triggers the auth flow on the same client/session.

    This happens when the user-supplied ``refresh()`` callable makes an HTTP
    request through the **same** client or session that has this auth handler
    attached, and that request also receives a 401 response — causing the
    coordinator to re-enter its own lock from the same thread or coroutine.

    ``threading.Lock`` and ``asyncio.Lock`` are **not reentrant**, so this
    would cause a permanent deadlock.  Instead of silently hanging, this
    exception is raised immediately with a clear error message.

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

    Stream, generator, and file-like request bodies are consumed on the first
    send and cannot be re-read for a retry.  Rather than silently sending an
    empty body (causing data loss) or raising a cryptic low-level error, this
    exception is raised with a clear explanation.

    **Fix:** Either buffer the body into ``bytes`` before sending, or handle
    the 401/refresh logic at a higher layer that can re-construct the body::

        # Correct — buffer into bytes first
        data = my_stream.read()
        client.post("/upload", content=data)

        # Risky — generator body cannot be replayed on 401
        client.post("/upload", content=my_generator())
    """
