# Contributing to singleflight-auth

Thank you for your interest in contributing!

## Development Setup

```bash
# Clone the repo
git clone https://github.com/alibeg-begow/singleflight_auth.git
cd singleflight_auth

# Install uv (if not already installed)
# https://docs.astral.sh/uv/getting-started/installation/

# Install all dev dependencies
uv sync --all-extras --dev

# Run the test suite
uv run pytest -v

# Type check
uv run mypy src --strict

# Lint
uv run ruff check .
uv run ruff format .
```

## Running Tests

```bash
# All tests
uv run pytest -v

# Concurrency stress tests only
uv run pytest tests/test_concurrency_stress.py -v

# With coverage
uv run pytest --cov=singleflight_auth --cov-report=term-missing
```

## Code Style

- **Formatter**: `ruff format` (line length 100)
- **Linter**: `ruff check` (E, F, I, UP, B, ASYNC rules)
- **Type checker**: `mypy --strict`

All three must pass cleanly before submitting a PR.

## Commit Messages

Please use [Conventional Commits](https://www.conventionalcommits.org/) format:

```
feat: add requests integration
fix: prevent double refresh on asyncio.Lock contention
test: add stress test for 50 concurrent 401s
docs: update README with async example
```

## Scope

Please read the "Out of scope" section in `README.md` before proposing new features.
In particular, OAuth2 flow implementations, token storage, multi-process locks,
aiohttp support, and proactive token refresh are **intentionally excluded from v0.1**.

## Pull Request Checklist

- [ ] Tests added/updated for the change
- [ ] `uv run pytest -v` passes
- [ ] `uv run mypy src --strict` passes (zero errors)
- [ ] `uv run ruff check .` passes
- [ ] CHANGELOG.md updated under `[Unreleased]`
