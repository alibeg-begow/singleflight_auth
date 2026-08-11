"""Shared pytest fixtures for singleflight-auth tests."""
from __future__ import annotations

from collections.abc import Iterator

import pytest
from pytest_httpserver import HTTPServer


@pytest.fixture()
def fresh_token_store() -> dict[str, str]:
    """A simple in-memory token store for tests."""
    return {"access": "initial-token"}


@pytest.fixture()
def threaded_httpserver() -> Iterator[HTTPServer]:
    """An HTTP server that handles requests **concurrently**.

    The stock ``httpserver`` fixture runs single-threaded, so it answers one
    request at a time.  That silently serialises any "N concurrent 401s" test:
    the first caller refreshes before the others have even received their 401,
    so they never contend for the coordinator lock — and a test meant to prove
    a locking bug passes against the buggy code.

    Use this fixture (usually with a :class:`threading.Barrier` in the handler)
    whenever a test needs real contention rather than the appearance of it.
    """
    server = HTTPServer(threaded=True)
    server.start()
    yield server
    server.clear()
    if server.is_running():
        server.stop()
