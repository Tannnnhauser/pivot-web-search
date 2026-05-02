# Contributing

## Setup

```sh
git clone <repo-url>
cd pivot-web-search
uv sync --extra dev
```

## Running Tests

```sh
pytest -m "not integration"     # fast offline tests
pytest                          # all tests (needs API keys + network)
```

## Code Style

This project uses [ruff](https://docs.astral.sh/ruff/) for linting:

```sh
ruff check .
ruff format --check .
```

Target: Python 3.10+, line length 120.

## Pull Requests

- One logical change per PR.
- Include tests for new functionality.
- All offline tests must pass before merge.
- Keep commit messages concise — describe the *why*, not the *what*.

## Reporting Issues

Open an issue on the repository. Include:

- Python version (`python3 --version`)
- OS and shell
- Steps to reproduce
- Expected vs. actual behavior
