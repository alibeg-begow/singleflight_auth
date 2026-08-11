# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-08-10

A correctness release. Several of the fixes below change observable behaviour;
read "Changed" before upgrading.

### Fixed
- **Packaging: the optional extras were effectively mandatory.** `__init__.py`
  imported both integrations unconditionally, so `pip install singleflight-auth[httpx]`
  failed at `import singleflight_auth` with `ModuleNotFoundError: No module named 'requests'`.
  Only installing *both* extras worked. Integration classes are now imported lazily
  (PEP 562); a missing extra raises an `ImportError` naming the extra to install.
- **`AsyncSingleFlightAuth` was permanently bound to the first event loop it was
  contended on.** `asyncio.Lock` binds to the running loop on contention, so the
  documented "create once, share everywhere" usage raised
  `RuntimeError: ... is bound to a different event loop` on any later loop
  (`pytest-asyncio`, notebooks, repeated `asyncio.run()`, thread-per-loop servers).
  Locks are now kept per event loop. The failure was contention-only, so it never
  showed up on single requests.
- **The single-flight guarantee depended on the token *value* changing.** The
  double-check compared token strings, so an idempotent/cached refresh endpoint,
  disabled token rotation, or a fixed staging token dropped protection to zero —
  N concurrent 401s produced N refreshes (measured: exactly 20/20 on 0.1.3 with
  a barriered 20-request wave; 1/20 now). Coordination is now keyed on an
  internal generation counter first, with the token comparison kept as a
  *secondary* check so tokens renewed outside this library (background TTL
  thread, sidecar, another process) are still honoured without a refresh.
- **A failing `refresh()` produced a thundering herd on the auth server.** Every
  waiter took the lock, saw nothing had changed, and called the already-failing
  endpoint again. The failure is now cached for `refresh_error_cooldown` seconds
  (default 1.0) and replayed to waiters in that window.
- **Mixing the sync and async classes sent requests with no `Authorization` header
  at all** — no exception, no warning. Each class now raises `TypeError` on the
  flow it does not implement.
- **`files=` (multipart) uploads always failed with `NonReplayableBodyError` on 401.**
  httpx rewinds multipart fields on every iteration, so seekable uploads are
  replayable; they are now retried normally. Generators and non-seekable files
  still raise.
- **`is_unauthorized` could not read the response body.** `requires_response_body`
  was never set, so a body-inspecting predicate raised `httpx.ResponseNotRead`.
  The body is now read on demand the first time a predicate reaches for it, and
  buffered up front from then on — so body-based detection works out of the box
  while a status-code-only predicate never forces buffering and `client.stream()`
  stays lazy. Two limits of that probe are documented and covered by tests: the
  predicate runs twice on the single response that first reaches for the body
  (once per auth instance, so a side-effecting predicate double-fires once), and
  a predicate that catches its **own** exceptions swallows the `ResponseNotRead`
  signal and silently reports "authorized". Pass `requires_response_body=True`
  to skip the probe entirely in both cases.
- **`is_unauthorized` was evaluated twice per response** (once in the retry loop
  condition, once in the final check) — for *every* response, in 0.1.3 too. It
  now runs exactly once per response. Behaviour change for predicates that
  count, log or otherwise have side effects.
- **Coordination now checks the generation first and the token value second.**
  A pure generation counter would have made refreshes performed *outside* this
  library invisible — a background TTL thread or a sibling process writing the
  shared token store would have caused a redundant refresh on every 401. The
  hybrid keeps K-3's value-independence and 0.1.x's external-renewal awareness.
- **`ReentrantRefreshError` never reached the caller.** It was caught by the
  blanket `except Exception` in the refresh wrapper and re-raised as
  `RefreshFailedError`, making the documented `except ReentrantRefreshError`
  handler dead code. Library control exceptions now pass through unwrapped.
- **`requests`: 401 connections stayed checked out of the pool for the whole
  refresh.** The response body is now consumed before `resolve()`, not after.
- `get_token()` returning `None`/`""` put the literal `"Bearer None"` on the wire;
  no auth header is sent instead.
- `_is_replayable_body`'s docstring contradicted its code about `dict` bodies.

### Added
- `refresh_wait_timeout` — bound the wait for *another* caller's in-flight refresh;
  raises the new `RefreshTimeoutError`, a **subclass of `RefreshFailedError`** so
  existing `except RefreshFailedError` handlers keep catching it. Named for what
  it does: it bounds the *waiting* only. A blocking `refresh()` cannot be
  interrupted from outside (and killing it could leave the caller's token store
  half-written), so the winner always runs to completion — in the async
  coordinator too, where `wait_for` wraps only the lock acquisition. If the
  winner hangs forever it holds the lock forever and every later request fails
  with `RefreshTimeoutError`: a visible degradation instead of a silent
  deadlock, but still a symptom — always give `refresh()` its own I/O timeout.
- `refresh_error_cooldown` — tune or disable (`0`) the failure cooldown.
- `raise_on_max_retries=False` — return the final unauthorized response instead
  of raising.
- `MaxRetriesExceededError.response` — the final response, so `WWW-Authenticate`,
  `Retry-After` and the error body stay reachable on a permanent 401.
- `header_name` / `scheme` — support `Token`, `JWT`, `X-API-Key` and bare-token
  headers instead of hard-coded `Bearer`.
- `requires_response_body` — explicit override of the lazy probe: `True` always
  buffers (deterministic, no double-fire), `False` forbids buffering and raises
  a `RuntimeError` naming the flag if a predicate reaches for the body anyway.
- Regression tests for every issue above, including the two exceptions added in
  0.1.3 that shipped with no test coverage at all.
- CI: per-install-path import smoke tests, a minimum-dependency-version job, a
  repeated-run job for the race-sensitive suites, `mypy --strict` over `tests/`,
  and a 95% coverage floor.

### Changed
- **BREAKING**: `max_retries=0` now means "do not refresh, return the unauthorized
  response" instead of raising `MaxRetriesExceededError` without ever retrying.
- **BREAKING**: negative `max_retries` raises `ValueError` instead of silently
  behaving like `0`.
- **BREAKING** (internal): `SyncCoordinator.resolve` / `AsyncCoordinator.resolve`
  take `(seen_generation, stale_token)` and return `(token, generation)`.
  These live in the private `_core` module and are not part of the public API.
- The package version is now single-sourced from `__init__.__version__`.
  `hatch.version` reads that literal with a regex, so the dependency runs one
  way only and `__version__` stays readable from an uninstalled checkout —
  pinned by a test, since the suite itself always runs against an installed
  package and would not otherwise notice a switch to `importlib.metadata`.
- Tests: `conftest.py` gained a `threaded_httpserver` fixture. The stock
  `httpserver` fixture is single-threaded, which silently serialises "N
  concurrent 401s" scenarios — a concurrency test written against it can pass
  on code that has the bug. Every regression test here was run against the
  0.1.3 source to confirm it actually fails there.

## [0.1.3] - 2026-08-07

### Added
- Deadlock detection: raise `ReentrantRefreshError` when `refresh()` uses the same client/session
- Stream body protection: raise `NonReplayableBodyError` on 401 retry to prevent silent data loss

### Fixed
- Fixed a connection leak in `requests` flow when `MaxRetriesExceededError` is raised
- Updated documentation with important usage notes and corrected license text

## [0.1.2] - 2026-08-03

### Changed
- **BREAKING**: Dropped support for Python 3.9 (minimum supported version is now 3.10)
- Changed license from CC BY-ND 4.0 to MIT to allow modifications in forks

### Fixed
- Fixed strict `mypy` error in CI by adding `types-requests` and `type: ignore` to `requests_auth.py`
- Fixed a trivially true assertion in `test_edge_cases.py`

## [0.1.0] - 2026-08-02

### Added
- `SyncCoordinator` and `AsyncCoordinator` — framework-agnostic double-checked locking core
- `SingleFlightAuth` for `httpx.Client` (sync)
- `AsyncSingleFlightAuth` for `httpx.AsyncClient` (async)
- `RequestsSingleFlightAuth` for `requests.Session`
- `RefreshFailedError` and `MaxRetriesExceededError` exceptions
- `py.typed` marker for PEP 561 compliance
- Concurrency stress tests (50 concurrent 401s → exactly 1 refresh call)
- Full type annotations with `mypy --strict` passing
- CI workflow for Python 3.10–3.13
