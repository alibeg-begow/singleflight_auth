# singleflight-auth

> **Single-flight token refresh for `httpx` and `requests`**: when 50 parallel requests all get a 401, your `refresh()` function is called **exactly once**.

[![PyPI version](https://img.shields.io/pypi/v/singleflight-auth.svg)](https://pypi.org/project/singleflight-auth/)
[![Python versions](https://img.shields.io/pypi/pyversions/singleflight-auth.svg)](https://pypi.org/project/singleflight-auth/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![CI](https://github.com/alibeg-begow/singleflight_auth/actions/workflows/ci.yml/badge.svg)](https://github.com/alibeg-begow/singleflight_auth/actions/workflows/ci.yml)

---

## 30-second example

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

# Even if 50 parallel requests hit a 401, refresh is called exactly once.
```

---

## Why this library?

| Library | Scope | Why it's different |
|---|---|---|
| `httpx` (official) | `Auth.auth_flow` pattern documented | No lock/queue — DIY |
| `httpx-auth` | Full OAuth2 client (auth code, PKCE, client credentials, browser) | Heavy, spec-bound; doesn't fit custom refresh endpoints |
| `requests_oauth2client` | Full OAuth2/OIDC for `requests` | Full spec implementation — overkill if you only want "call my refresh on 401" |
| `singleflight` (PyPI) | General call-coalescing (Go's groupcache port) | Not HTTP/auth-specific; no 401 detection or retry |

**Our position:** If you need full OAuth2 flows, use `httpx-auth` or `requests_oauth2client`. If you already have your own refresh logic and just want to prevent parallel 401s from stomping each other — this library is for you.

---

## Installation

```bash
# For httpx users:
pip install singleflight-auth[httpx]

# For requests users:
pip install singleflight-auth[requests]

# Both:
pip install singleflight-auth[httpx,requests]
```

---

## How it works

The core uses **double-checked locking**:

```
1. Request gets a 401; remembers the stale token T_stale
2. Acquires lock (threads block; coroutines yield without blocking the event loop)
3. After acquiring: re-reads current token T_current
4. If T_current != T_stale  → someone else already refreshed; skip refresh, use T_current
   If T_current == T_stale  → you're the winner; call refresh(), save result
5. Release lock
6. Retry request with new token
```

This means N concurrent requests racing on a 401 result in **exactly 1** refresh call, with the remaining N-1 getting the already-refreshed token for free.

---

## API Reference

### `SingleFlightAuth` (httpx sync)

```python
from singleflight_auth import SingleFlightAuth

auth = SingleFlightAuth(
    get_token=...,        # Callable[[], str] — returns current token
    refresh=...,          # Callable[[], str] — fetches a new token, saves it, returns it
    is_unauthorized=...,  # Callable[[httpx.Response], bool], default: lambda r: r.status_code == 401
    max_retries=1,        # int — max retry attempts after a refresh
)
```

### `AsyncSingleFlightAuth` (httpx async)

```python
from singleflight_auth import AsyncSingleFlightAuth

auth = AsyncSingleFlightAuth(
    get_token=...,        # Callable[[], str]
    refresh=...,          # Callable[[], Awaitable[str]] — async refresh function
    is_unauthorized=...,  # Callable[[httpx.Response], bool]
    max_retries=1,
)
```

### `RequestsSingleFlightAuth` (requests)

```python
from singleflight_auth import RequestsSingleFlightAuth

auth = RequestsSingleFlightAuth(
    get_token=...,        # Callable[[], str]
    refresh=...,          # Callable[[], str]
    is_unauthorized=...,  # Callable[[requests.Response], bool]
    max_retries=1,
)
```

### Exceptions

```python
from singleflight_auth import RefreshFailedError, MaxRetriesExceededError

try:
    client.get("/protected")
except RefreshFailedError as e:
    # refresh() raised an exception — log out the user
    print(f"Refresh failed: {e.__cause__}")
except MaxRetriesExceededError:
    # Still getting 401 after max_retries attempts
    pass
```

---

## Limitations

Intentionally out of scope for v0.1:

- **No OAuth2 flow implementation** — bring your own `refresh()` logic
- **No token storage** — manage tokens yourself via `get_token`/`refresh` callbacks  
- **Single-process only** — uses in-process locks (`threading.Lock` / `asyncio.Lock`); does not work across multiple processes or machines
- **No `aiohttp` support** — `httpx` + `requests` only
- **Reactive only** — refreshes on 401, no proactive expiry tracking

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

---

## License

MIT — see [LICENSE](LICENSE).
