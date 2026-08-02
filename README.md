# singleflight-auth

> **Single-flight token refresh for `httpx` and `requests`**: when hundreds or thousands of parallel requests all get a 401, your `refresh()` function is called **exactly once**.

[![PyPI version](https://img.shields.io/pypi/v/singleflight-auth.svg?v=1)](https://pypi.org/project/singleflight-auth/)
[![Python versions](https://img.shields.io/pypi/pyversions/singleflight-auth.svg?v=1)](https://pypi.org/project/singleflight-auth/)
[![License: CC BY-ND 4.0](https://img.shields.io/badge/License-CC%20BY--ND%204.0-lightgrey.svg)](https://creativecommons.org/licenses/by-nd/4.0/)
[![CI](https://github.com/alibeg-begow/singleflight_auth/actions/workflows/ci.yml/badge.svg)](https://github.com/alibeg-begow/singleflight_auth/actions/workflows/ci.yml)
[![Typed](https://img.shields.io/badge/typing-typed-blue.svg)](https://peps.python.org/pep-0561/)

---

## 30-Second Example

```python
import httpx
from singleflight_auth import SingleFlightAuth

def get_access_token() -> str:
    return token_store.get("access")

def refresh_access_token() -> str:
    resp = httpx.post(
        "https://api.example.com/auth/refresh",
        json={"refresh_token": token_store.get("refresh")},
    )
    resp.raise_for_status()
    data = resp.json()
    token_store.set("access", data["access_token"])
    return data["access_token"]

auth = SingleFlightAuth(get_token=get_access_token, refresh=refresh_access_token)
client = httpx.Client(auth=auth, base_url="https://api.example.com")

# Even if unlimited parallel requests hit a 401, refresh is called exactly once.
```

### Async variant

```python
from singleflight_auth import AsyncSingleFlightAuth

async def async_refresh() -> str:
    async with httpx.AsyncClient() as c:
        resp = await c.post("https://api.example.com/auth/refresh", json={...})
        resp.raise_for_status()
        data = resp.json()
        token_store.set("access", data["access_token"])
        return data["access_token"]

auth = AsyncSingleFlightAuth(get_token=get_access_token, refresh=async_refresh)
async with httpx.AsyncClient(auth=auth) as client:
    # Whether it's 10, 50, or 10,000 requests, they are all coordinated.
    responses = await asyncio.gather(*[client.get("/api/data") for _ in range(500)])
    # refresh() was called exactly once — all 500 got the fresh token.
```

### `requests` variant

```python
import requests
from singleflight_auth import RequestsSingleFlightAuth

auth = RequestsSingleFlightAuth(get_token=get_access_token, refresh=refresh_access_token)
session = requests.Session()
session.auth = auth
response = session.get("https://api.example.com/protected")
```

---

## Why This Library?

| Library | Scope | Why it's different |
|---|---|---|
| `httpx` (official) | `Auth.auth_flow` pattern documented | No lock/queue — DIY, no concurrency safety |
| [`httpx-auth`](https://pypi.org/project/httpx-auth/) | Full OAuth2 client (auth code, PKCE, client credentials, browser integration) | Heavy, spec-bound; 541K+ weekly downloads but doesn't fit custom refresh endpoints |
| [`requests_oauth2client`](https://pypi.org/project/requests-oauth2client/) | Full OAuth2/OIDC for `requests` | Full spec implementation — overkill if you only want "call my refresh on 401" |
| [`singleflight`](https://pypi.org/project/singleflight/) | General call-coalescing (Go's groupcache port) | Not HTTP/auth-specific; no 401 detection or retry logic |

**Our position:** If you need full OAuth2 flows, use `httpx-auth` or `requests_oauth2client` — they're excellent. If you already have your own refresh logic (JWT endpoint, custom auth API, or even an OAuth2 token endpoint you call manually) and just want to **prevent parallel 401s from stomping each other** — this library is for you.

> *"Bring your own refresh logic — we handle the concurrency."*

---

## Installation

```bash
# For httpx users (sync + async):
pip install singleflight-auth[httpx]

# For requests users:
pip install singleflight-auth[requests]

# Both:
pip install singleflight-auth[httpx,requests]
```

---

## How It Works

The core uses **double-checked locking** — a well-known concurrency pattern adapted for token refresh:

```
                    ┌──────────────────────────────────────────┐
                    │         50 requests hit 401              │
                    └──────────────────┬───────────────────────┘
                                       │
                    ┌──────────────────▼───────────────────────┐
                    │     Each remembers stale token T_stale   │
                    └──────────────────┬───────────────────────┘
                                       │
                    ┌──────────────────▼───────────────────────┐
                    │        Acquire lock (one wins)           │
                    │   threads: block  │  coroutines: yield   │
                    └──────────────────┬───────────────────────┘
                                       │
                    ┌──────────────────▼───────────────────────┐
                    │    Re-read current token T_current       │
                    └───────┬──────────────────────┬───────────┘
                            │                      │
               T_current ≠ T_stale       T_current == T_stale
                            │                      │
                    ┌───────▼──────┐      ┌────────▼──────────┐
                    │ Skip refresh │      │ YOU are the winner │
                    │ Use T_current│      │ Call refresh()     │
                    └───────┬──────┘      └────────┬──────────┘
                            │                      │
                    ┌───────▼──────────────────────▼───────────┐
                    │           Release lock                   │
                    │   Retry request with fresh token         │
                    └──────────────────────────────────────────┘

Result: N concurrent 401s → exactly 1 refresh() call
        The other N-1 get the fresh token for free.
```

**Key design decisions:**

- **`SyncCoordinator`** uses `threading.Lock` — threads block while waiting.
- **`AsyncCoordinator`** uses `asyncio.Lock` — coroutines **yield** without blocking the event loop.
- The two are never mixed: `httpx.Client` uses the sync path, `httpx.AsyncClient` uses the async path.

---

## API Reference

### `SingleFlightAuth` — httpx sync

```python
from singleflight_auth import SingleFlightAuth

auth = SingleFlightAuth(
    get_token: Callable[[], str],          # Returns the current token
    refresh: Callable[[], str],            # Fetches a new token, saves it, returns it
    is_unauthorized: Callable[              # Optional: customize 401 detection
        [httpx.Response], bool
    ] = lambda r: r.status_code == 401,
    max_retries: int = 1,                  # Max retry attempts after refresh
)
```

### `AsyncSingleFlightAuth` — httpx async

```python
from singleflight_auth import AsyncSingleFlightAuth

auth = AsyncSingleFlightAuth(
    get_token: Callable[[], str],
    refresh: Callable[[], Awaitable[str]],  # Must be an async function
    is_unauthorized: Callable[[httpx.Response], bool] = ...,
    max_retries: int = 1,
)
```

### `RequestsSingleFlightAuth` — requests

```python
from singleflight_auth import RequestsSingleFlightAuth

auth = RequestsSingleFlightAuth(
    get_token: Callable[[], str],
    refresh: Callable[[], str],
    is_unauthorized: Callable[[requests.Response], bool] = ...,
    max_retries: int = 1,
)

session = requests.Session()
session.auth = auth
```

### Exceptions

| Exception | When |
|---|---|
| `RefreshFailedError` | Your `refresh()` callable raised an exception, or returned an empty/None token |
| `MaxRetriesExceededError` | Still getting 401 after `max_retries` refresh attempts |

```python
from singleflight_auth import RefreshFailedError, MaxRetriesExceededError

try:
    response = client.get("/protected")
except RefreshFailedError as e:
    # refresh() raised — log out the user, redirect to login
    logger.error(f"Token refresh failed: {e.__cause__}")
except MaxRetriesExceededError:
    # Server keeps returning 401 even after refresh — something is very wrong
    logger.error("Max retries exceeded, giving up")
```

---

## The Proof — Concurrency Stress Test

This is the test that proves the library's core promise. It's in the test suite and runs in CI:

```python
@pytest.mark.asyncio
async def test_only_one_refresh_under_50_concurrent_401s(httpserver):
    state = {"token": "expired", "refresh_calls": 0}

    async def refresh() -> str:
        await asyncio.sleep(0.05)  # simulate real network latency
        state["refresh_calls"] += 1
        state["token"] = "fresh"
        return "fresh"

    auth = AsyncSingleFlightAuth(get_token=lambda: state["token"], refresh=refresh)

    async with httpx.AsyncClient(auth=auth, base_url=httpserver.url_for("/")) as client:
        responses = await asyncio.gather(*[client.get("/protected") for _ in range(50)])

    assert all(r.status_code == 200 for r in responses)
    assert state["refresh_calls"] == 1  # ← THIS IS THE ENTIRE POINT
```

---

## Limitations

Intentionally out of scope for v0.1:

| What | Why |
|---|---|
| **No OAuth2 flow implementation** | Bring your own `refresh()` logic — we don't dictate your auth scheme |
| **No token storage** | Manage tokens yourself via `get_token`/`refresh` callbacks |
| **Single-process only** | Uses in-process locks (`threading.Lock` / `asyncio.Lock`); does not work across multiple processes or machines (e.g., Gunicorn workers) |
| **No `aiohttp` support** | `httpx` + `requests` only for v0.1 |
| **Reactive only** | Refreshes on 401; no proactive TTL-based refresh |

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and guidelines.

```bash
# Quick start for contributors:
git clone https://github.com/alibeg-begow/singleflight_auth.git
cd singleflight_auth
uv sync --all-extras --dev
uv run pytest -v
uv run mypy src --strict
uv run ruff check .
```

---

## License

This project is licensed under the **Creative Commons Attribution-NoDerivatives 4.0 International License** (CC BY-ND 4.0).

You are free to share and use this software, but you may not distribute modified versions. See [LICENSE](LICENSE) for the full text, or visit [creativecommons.org/licenses/by-nd/4.0](https://creativecommons.org/licenses/by-nd/4.0/).
