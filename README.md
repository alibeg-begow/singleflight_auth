# singleflight-auth

> **Single-flight token refresh for `httpx` and `requests`**: when parallel requests hit a 401, your `refresh()` function is called **exactly once** per process. Coordination uses in-process locks (see [Limitations](#limitations)).

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

**Our position:** If you need full OAuth2 flows, use `httpx-auth` or `requests_oauth2client`. If you already have your own refresh logic and simply want to **prevent concurrent 401s from stampeding your auth server**, this library is for you.

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
                    │   Each remembers the generation G it saw │
                    └──────────────────┬───────────────────────┘
                                       │
                    ┌──────────────────▼───────────────────────┐
                    │        Acquire lock (one wins)           │
                    │   threads: block  │  coroutines: yield   │
                    └──────────────────┬───────────────────────┘
                                       │
                    ┌──────────────────▼───────────────────────┐
                    │  Re-check: generation, then token value  │
                    └───────┬──────────────────────┬───────────┘
                            │                      │
                 generation moved,          both unchanged
                 or token changed                  │
                            │                      │
                    ┌───────▼──────┐      ┌────────▼───────────┐
                    │ Skip refresh │      │ YOU are the winner │
                    │ Use get_token│      │ Call refresh(), G+1│
                    └───────┬──────┘      └────────┬───────────┘
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
- **`AsyncCoordinator`** uses `asyncio.Lock` — coroutines yield without blocking the event loop. One lock is kept per event loop, ensuring safety across repeated `asyncio.run()` calls and testing suites.
- **Strict runtime validation**: Pairing sync and async classes incorrectly raises `TypeError` instead of sending unauthenticated requests.
- **Generation counter + value check**: Coordination uses an internal generation counter first (handling idempotent refresh endpoints), with token value as a secondary check (handling external renewals like background workers).
- **Error caching**: Failed refreshes are cached for `refresh_error_cooldown` seconds and replayed to queued waiters, preventing auth server overload.

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
    ] | None = None,                        # default: r.status_code == 401
    max_retries: int = 1,                  # Retry attempts after refresh; 0 disables refresh
    *,
    header_name: str = "Authorization",    # e.g. "X-API-Key"
    scheme: str = "Bearer",                # "" sends the bare token
    raise_on_max_retries: bool = True,     # False → return the 401 instead of raising
    refresh_wait_timeout: float | None = None,  # Bound the *wait* for someone else's refresh
    refresh_error_cooldown: float = 1.0,   # Replay a failed refresh for N seconds
    requires_response_body: bool | None = None,  # None = buffer only if a predicate needs it
)
```

`AsyncSingleFlightAuth` takes the same arguments, with `refresh: Callable[[], Awaitable[str]]`.
`RequestsSingleFlightAuth` takes the same arguments minus `requires_response_body`
(`requests` always buffers the body before hooks run).

```python
from singleflight_auth import AsyncSingleFlightAuth, RequestsSingleFlightAuth

auth = AsyncSingleFlightAuth(get_token=get_access_token, refresh=async_refresh)

session = requests.Session()
session.auth = RequestsSingleFlightAuth(get_token=get_access_token, refresh=refresh_access_token)
```

#### Notable parameter semantics

- **`max_retries=0`** means *"never refresh, hand me the unauthorized response"*. It does not raise.
- **`get_token()` returning `None` or `""`** sends **no** auth header (rather than `"Bearer None"`), so the first request before login naturally 401s and triggers a refresh.
- **`is_unauthorized` may read `response.content`.** The body is buffered on demand only if your predicate accesses it, keeping `client.stream()` lazy. Use `requires_response_body=True` to always buffer, or `False` to forbid body reading.
  > **Note on auto-detection**: Avoid catching broad exceptions (`except Exception`) inside your predicate, as swallowing `ResponseNotRead` prevents buffering detection.
- **`refresh_wait_timeout` bounds the *wait*, not `refresh()`.** Caps how long other callers queue behind an in-flight refresh. Always set an explicit I/O timeout inside `refresh()` to prevent hanging.
- **`refresh_error_cooldown`** stops a failing auth server from receiving one retry per pending request. Set to `0` to attempt a refresh on every request.

### Exceptions

| Exception | When |
|---|---|
| `RefreshFailedError` | Your `refresh()` callable raised an exception, or returned an empty/None token |
| `RefreshTimeoutError` | `refresh_wait_timeout` elapsed while waiting for an in-flight refresh (subclass of `RefreshFailedError`) |
| `MaxRetriesExceededError` | Still getting 401 after `max_retries` refresh attempts; carries `.response` |
| `ReentrantRefreshError` | `refresh()` triggered the auth flow on the same client/session (would deadlock) |
| `NonReplayableBodyError` | Request body is a generator or non-seekable stream that cannot be replayed for retry |

```python
from singleflight_auth import (
    RefreshFailedError,
    RefreshTimeoutError,
    MaxRetriesExceededError,
    ReentrantRefreshError,
    NonReplayableBodyError,
)

try:
    response = client.get("/protected")
except RefreshTimeoutError:
    # An in-flight refresh took too long — fail fast instead of parking forever
    logger.error("Token refresh timed out")
except RefreshFailedError as e:
    # refresh() raised — log out the user, redirect to login
    logger.error(f"Token refresh failed: {e.__cause__}")
except MaxRetriesExceededError as e:
    # Server keeps returning 401 even after refresh — inspect why
    logger.error("Still %s: %s", e.response.status_code, e.response.headers.get("WWW-Authenticate"))
except ReentrantRefreshError:
    # refresh() accidentally used the same client — fix your refresh() code
    logger.error("refresh() must use a separate client!")
except NonReplayableBodyError:
    # Streaming upload got a 401 — buffer the body or handle differently
    logger.error("Cannot retry streaming uploads")
```

A *permanent* 401 (revoked account, missing scope) is not solved by refreshing.
If you would rather handle it as a normal response than as an exception:

```python
auth = SingleFlightAuth(get_token=..., refresh=..., raise_on_max_retries=False)
response = client.get("/admin-only")   # returns the 401 instead of raising
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

### Don't pair the sync class with an async client

`SingleFlightAuth` is sync-only and `AsyncSingleFlightAuth` is async-only. Since the names differ by one word, a mismatch is easy to make — so each class raises `TypeError` on the flow it does not implement instead of letting the request through unauthenticated.

```python
auth = SingleFlightAuth(...)
async with httpx.AsyncClient(auth=auth) as client:
    await client.get("/data")   # TypeError: SingleFlightAuth is sync-only
```

### Generator and non-seekable bodies are not retry-safe

If the request body is a generator or a non-seekable stream, it is consumed on the first send and cannot be replayed. Instead of silently sending an empty body, `NonReplayableBodyError` is raised. Buffer the body into `bytes` before sending:

```python
# Correct — body is bytes, safe to retry
data = my_file.read()
client.post("/upload", content=data)

# Also fine — httpx rewinds seekable multipart files on every send
client.post("/upload", files={"f": open("photo.jpg", "rb")})

# Risky — generator body cannot be replayed on 401
client.post("/upload", content=my_generator())
```

### Bound the refresh wait

`refresh()` with no timeout of its own can park every other thread or coroutine indefinitely. Give it one — and optionally a ceiling on how long waiters queue behind it:

```python
def refresh() -> str:
    resp = httpx.post("https://auth.example.com/token", json={...}, timeout=10.0)
    return resp.json()["access_token"]

auth = SingleFlightAuth(get_token=..., refresh=refresh, refresh_wait_timeout=15.0)
```

---

## Limitations

Intentionally out of scope:

| What | Why |
|---|---|
| **Single-process only** | Uses in-process locks (`threading.Lock` / `asyncio.Lock`). With 8 Gunicorn/Uvicorn workers you get up to 8 refreshes — one per worker — not one overall. Cross-process coordination (e.g. Redis) is not implemented |
| **No OAuth2 flow implementation** | Bring your own `refresh()` logic — we don't dictate your auth scheme |
| **No token storage** | Manage tokens yourself via `get_token`/`refresh` callbacks |
| **No `aiohttp` support** | `httpx` + `requests` only |
| **Reactive only** | Refreshes on 401; no proactive TTL-based refresh |
| **Generator/non-seekable bodies not retried** | They raise `NonReplayableBodyError` on 401 instead of silently losing data. Seekable multipart uploads *are* retried |

---

## Contributing

See [CONTRIBUTING.md](https://github.com/alibeg-begow/singleflight_auth/blob/main/CONTRIBUTING.md) for development setup and guidelines.

```bash
# Quick start for contributors:
git clone https://github.com/alibeg-begow/singleflight_auth.git
cd singleflight_auth
uv sync --all-extras --dev
uv run pytest -v
uv run mypy src tests --strict
uv run ruff check .
```

---

## License

This project is licensed under the [MIT License](https://github.com/alibeg-begow/singleflight_auth/blob/main/LICENSE).
