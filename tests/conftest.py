"""Shared pytest fixtures for singleflight-auth tests."""
from __future__ import annotations

import pytest


@pytest.fixture()
def fresh_token_store() -> dict[str, str]:
    """A simple in-memory token store for tests."""
    return {"access": "initial-token"}
