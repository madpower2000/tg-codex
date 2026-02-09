# Repository Guidelines

## Project Structure & Module Organization
- Main package: `codex_telegram_bot/`
- Tests: `codex_telegram_bot/tests/`
- Runtime docs/deps: `README.md`, `pyproject.toml`

## Build, Test, and Development Commands
- Sync environment: `uv sync`
- Run app: `uv run python -m codex_telegram_bot`
- Run tests: `uv run pytest -q`

## Coding Style & Naming Conventions
- Use Python 3.12 features only.
- Keep changes minimal and scoped to the task.
- Prefer explicit typing and clear async boundaries.

## Testing Guidelines
- Add/update unit tests in `codex_telegram_bot/tests/` for behavior changes.
- Run `uv run pytest -q` before handoff.

## Commit & Pull Request Guidelines
- Keep commit scopes small and descriptive.
- Include test and checker results in PR notes.

## Environment Notes
- Use `uv` package manager for all Python workflows.
- Python is fixed to 3.12 via `.python-version`.
- After dependency edits, run `uv sync` to refresh the environment.

## Static Typing Enforcement (`ty`)
Your primary responsibility is to preserve semantic correctness, interface stability, and execution safety.
For this purpose, `ty` is the mandatory static type checker for all Python code.
Always run static type checking after any Python code change:
- `uv run ty check <file>`

## Linting & Style (`ruff`)
After any Python code change, you MUST run:
- `uv run ruff check <file>`
