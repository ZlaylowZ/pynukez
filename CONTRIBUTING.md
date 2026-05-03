# Contributing

## Setup

```bash
git clone https://github.com/Nukez-xyz/pynukez.git
cd pynukez
pip install -e ".[dev]"
```

The `[dev]` extra pulls testing and lint tooling (pytest, pytest-asyncio, pytest-mock, black, isort, mypy, python-dotenv). The runtime dependencies — httpx, pynacl, base58, eth-account — install with the base package.

## Tests

```bash
pytest
```

Async tests run automatically via `pytest-asyncio` (configured in `pyproject.toml`).

## Code Style

```bash
black pynukez/ tests/
isort pynukez/ tests/
mypy pynukez/
```

Line length is 100. Black and isort are both configured in `pyproject.toml`.

## Pull Requests

1. Fork the repo and create a feature branch
2. Make your changes — keep them small and focused
3. Add or update tests
4. Run `pytest`, `black`, `isort`, and `mypy` before pushing
5. Open a PR against `main` on https://github.com/Nukez-xyz/pynukez

## Release Process

Releases are tagged on `main` and published to PyPI via the
`.github/workflows/publish.yml` workflow (PyPI trusted publishing, no
API tokens required). Bump `version` in `pyproject.toml`, `__version__`
in `pynukez/__init__.py`, and the `User-Agent` string in `pynukez/_http.py`
before tagging.
