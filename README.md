# singleflight-auth

> **Single-flight token refresh for `httpx` and `requests`**: when hundreds or thousands of parallel requests all get a 401, your `refresh()` function is called **exactly once**.

[![PyPI version](https://img.shields.io/pypi/v/singleflight-auth.svg?v=1)](https://pypi.org/project/singleflight-auth/)
[![Python versions](https://img.shields.io/pypi/pyversions/singleflight-auth.svg?v=1)](https://pypi.org/project/singleflight-auth/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
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
| `ReentrantRefreshError` | `refresh()` triggered the auth flow on the same client/session (would deadlock) |
| `NonReplayableBodyError` | Request body is a stream/generator/file that cannot be replayed for retry |

```python
from singleflight_auth import (
    RefreshFailedError,
    MaxRetriesExceededError,
    ReentrantRefreshError,
    NonReplayableBodyError,
)

try:
    response = client.get("/protected")
except RefreshFailedError as e:
    # refresh() raised — log out the user, redirect to login
    logger.error(f"Token refresh failed: {e.__cause__}")
except MaxRetriesExceededError:
    # Server keeps returning 401 even after refresh — something is very wrong
    logger.error("Max retries exceeded, giving up")
except ReentrantRefreshError:
    # refresh() accidentally used the same client — fix your refresh() code
    logger.error("refresh() must use a separate client!")
except NonReplayableBodyError:
    # Streaming upload got a 401 — buffer the body or handle differently
    logger.error("Cannot retry streaming uploads")
```

---

## Important Usage Notes

### `refresh()` must use a separate client

The `refresh()` callable must **never** use the same `httpx.Client` / `httpx.AsyncClient` / `requests.Session` that has this auth handler attached. If it does, and the refresh endpoint also returns 401, the coordinator will detect the reentrant lock acquisition and raise `ReentrantRefreshError`.

```python
# Correct — uses a standalone httpx.post() without auth
def refresh() -> str:
    resp = httpx.post("https://auth.example.com/token", json={...})
    return resp.json()["access_token"]

# Wrong — reuses the client that has SingleFlightAuth attached
def refresh() -> str:
    resp = client.post("https://auth.example.com/token")  # DANGER: same client!
    return resp.json()["access_token"]
```

### Share a single auth instance

The single-flight guarantee only works when all requests share the **same** auth instance. Each instance has its own lock, so creating a new `SingleFlightAuth(...)` per request defeats the entire purpose.

```python
# Correct — one instance shared everywhere
auth = SingleFlightAuth(get_token=..., refresh=...)
client = httpx.Client(auth=auth)

# Wrong — new instance per request means N refreshes instead of 1
for url in urls:
    auth = SingleFlightAuth(get_token=..., refresh=...)  # WRONG
    httpx.get(url, auth=auth)
```

### Stream/generator/file bodies are not retry-safe

If the request body is a generator, file handle, or other one-shot stream, it is consumed on the first send and cannot be replayed. Instead of silently sending an empty body, `NonReplayableBodyError` is raised. Buffer the body into `bytes` before sending:

```python
# Correct — body is bytes, safe to retry
data = my_file.read()
client.post("/upload", content=data)

# Risky — generator body cannot be replayed on 401
client.post("/upload", content=my_generator())
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
| **Stream bodies not retried** | Generator/file/stream request bodies raise `NonReplayableBodyError` on 401 instead of silently losing data |

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

This project is licensed under the [MIT License](LICENSE).
