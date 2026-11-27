# Repository Guidelines

## Project Structure

`src/agentrouter_prober/` contains the Python package. `cli.py` starts the service, `app.py` coordinates probe cycles, `clients.py` handles AgentRouter HTTP requests, and `bot.py` and `telegram.py` handle Telegram commands and notifications.

`tests/` holds the pytest suite, with shared fixtures in `tests/conftest.py`. The `Dockerfile` and `compose.yaml` define container deployment.

## Build, Test, and Development

Use Python 3.11 or newer.

- `python -m pip install -e ".[test]"` installs the package in editable mode with pytest.
- `python -m pytest tests/` runs the full suite.
- `agentrouter-prober` starts the service with the environment variables documented in `README.md`. Set `STATE_PATH` to a writable file path for local runs. Add `--once` to run one live cycle, which can send Telegram alerts.
- `docker compose build` builds the service image. Follow [Start with Docker](README.md#start-with-docker) for Windows and Linux deployment.

## Coding Style and Naming

Use four spaces per indentation level, `snake_case` for modules and functions, `PascalCase` for classes, and `UPPER_SNAKE_CASE` for constants and environment variables. Add type hints to new interfaces and follow the surrounding formatting.

## Testing Guidelines

Use pytest 8. Name files `test_<module>.py` and functions `test_<behavior>`. Run `python -m pytest tests/test_clients.py` for a focused check. Keep tests offline with `httpx.MockTransport`. Exercise relevant error and retry paths when changing request or notification handling.

## Commits and Pull Requests

Use an imperative commit subject like the initial commit, `Add AgentRouter Telegram model availability bot`. In pull requests, describe the behavior change, report tests and results, and link related issues when available. For Telegram UI changes, include a sanitized example reply or screenshot when it helps review.

## Configuration and Secrets

Document environment variable changes in `README.md` and the applicable `.env` example file. Keep live `.env` files and runtime state out of Git.
