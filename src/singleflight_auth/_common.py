"""Small helpers shared by the httpx and requests integrations."""

from __future__ import annotations

from collections.abc import Callable, MutableMapping

from ._exceptions import (
    MaxRetriesExceededError,
    NonReplayableBodyError,
    ReentrantRefreshError,
    RefreshFailedError,
)

#: Exceptions the library raises itself to signal a control-flow problem.
#: They must reach the caller untouched — wrapping them in
#: :class:`RefreshFailedError` makes ``except ReentrantRefreshError`` dead code.
CONTROL_EXCEPTIONS = (
    ReentrantRefreshError,
    MaxRetriesExceededError,
    NonReplayableBodyError,
    RefreshFailedError,
)


def validate_max_retries(max_retries: int) -> None:
    """Reject negative retry counts, which otherwise behave like ``0``."""
    if max_retries < 0:
        raise ValueError(f"max_retries must be >= 0, got {max_retries}")


def apply_token(
    headers: MutableMapping[str, str],
    header_name: str,
    scheme: str,
    token: str | None,
) -> None:
    """Write ``<scheme> <token>`` into *headers* under *header_name*.

    An empty scheme sends the raw token (for ``X-API-Key``-style headers).

    A falsy token writes **nothing**: ``get_token()`` legitimately returns
    ``None``/``""`` before the first login, and formatting that into
    ``"Bearer None"`` only produces a confusing wire value.  Leaving the
    headers untouched also preserves an ``Authorization`` header the caller
    set explicitly on the request.
    """
    if not token:
        return
    headers[header_name] = f"{scheme} {token}" if scheme else token


def guard_refresh(refresh: Callable[[], str], message: str) -> Callable[[], str]:
    """Wrap ``refresh`` so unexpected errors become :class:`RefreshFailedError`.

    Control exceptions pass through unchanged — see :data:`CONTROL_EXCEPTIONS`.
    """

    def _safe_refresh() -> str:
        try:
            res = refresh()
            if not res:
                raise ValueError("Token refresh callable returned empty or None token")
        except CONTROL_EXCEPTIONS:
            raise
        except Exception as exc:
            raise RefreshFailedError(message) from exc
        return res

    return _safe_refresh
