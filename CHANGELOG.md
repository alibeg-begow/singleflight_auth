# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.1] - 2026-08-02

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
