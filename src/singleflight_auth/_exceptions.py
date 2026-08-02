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
