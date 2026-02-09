# Codex Telegram Bridge

Small local bridge that connects Telegram chats to a local `codex app-server` session.

## What it does

- Maps each Telegram chat to one Codex thread (`chat_id -> threadId`) and persists it.
- Starts a Codex turn for each text message.
- Streams live agent output updates to Telegram with edit rate-limiting.
- Handles Codex approval requests with Telegram inline buttons (Accept / Decline).
- Restarts `codex app-server` automatically and resumes known threads.

## Requirements

- Python 3.12 (pinned via `.python-version`)
- [`uv` package manager](https://docs.astral.sh/uv/getting-started/installation/)
- [`codex` CLI installed and authenticated](https://github.com/openai/codex)
- Telegram bot token

## Setup

```bash
uv sync
```

Create a local `.env` in the project root (already ignored by `.gitignore`):

```dotenv
TELEGRAM_BOT_TOKEN=<your_telegram_bot_token>
TELEGRAM_ALLOWED_USERS=<telegram_user_id_comma_separated>
CODEX_MODEL=gpt-5.3-codex
CODEX_CWD=C:/Projects/tg-codex
STATE_PATH=./.codex_telegram_state.json
LOG_LEVEL=INFO
```

Environment variable reference:

- Required:
  - `TELEGRAM_BOT_TOKEN` - Telegram bot token.
  - `TELEGRAM_ALLOWED_USERS` - comma-separated Telegram user ids (example: `123456,987654`).
- Optional:
  - `CODEX_MODEL` - defaults to `gpt-5.3-codex` (fallback to `gpt-5.1-codex` during thread creation).
  - `CODEX_CWD` - defaults to current working directory.
  - `STATE_PATH` - defaults to `./.codex_telegram_state.json`.
  - `LOG_LEVEL` - defaults to `INFO`.

Run:

```bash
uv run --env-file .env python -m codex_telegram_bot
```

## Telegram commands

- `/start` - help text.
- `/new` - create and map a new Codex thread for this chat.
- `/reset` - clear this chat mapping.

Any other text message starts a turn in the mapped thread.

## State file

Default state file: `./.codex_telegram_state.json`

Stored per chat:

- `thread_id`
- `last_turn_id`
- pending approval metadata (`request_id`, `method`, `thread_id`, `turn_id`)

## Tests

```bash
uv run pytest -q
```

## Optional schema generation

```bash
codex app-server generate-json-schema --out ./schemas
```
