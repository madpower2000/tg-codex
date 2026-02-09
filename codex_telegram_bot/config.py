from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PRIMARY_MODEL_DEFAULT = "gpt-5.3-codex"
FALLBACK_MODEL_DEFAULT = "gpt-5.1-codex"


@dataclass(slots=True)
class Config:
    telegram_bot_token: str
    telegram_allowed_users: set[int]
    codex_model: str
    codex_fallback_model: str
    codex_cwd: Path
    state_path: Path
    log_level: str
    auto_approve_commands: bool
    auto_approve_file_changes: bool
    live_edit_interval_seconds: float = 0.8

    @classmethod
    def from_env(cls) -> "Config":
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise ValueError("TELEGRAM_BOT_TOKEN is required")

        allowed_users = parse_allowed_users(os.getenv("TELEGRAM_ALLOWED_USERS", ""))
        model = os.getenv("CODEX_MODEL", PRIMARY_MODEL_DEFAULT).strip() or PRIMARY_MODEL_DEFAULT
        cwd = Path(os.getenv("CODEX_CWD", ".")).resolve()
        state_path = Path(os.getenv("STATE_PATH", "./.codex_telegram_state.json")).resolve()
        log_level = os.getenv("LOG_LEVEL", "INFO").upper().strip() or "INFO"
        auto_approve_commands = parse_bool_env("CODEX_AUTO_APPROVE_COMMANDS", default=False)
        auto_approve_file_changes = parse_bool_env("CODEX_AUTO_APPROVE_FILE_CHANGES", default=False)

        return cls(
            telegram_bot_token=token,
            telegram_allowed_users=allowed_users,
            codex_model=model,
            codex_fallback_model=FALLBACK_MODEL_DEFAULT,
            codex_cwd=cwd,
            state_path=state_path,
            log_level=log_level,
            auto_approve_commands=auto_approve_commands,
            auto_approve_file_changes=auto_approve_file_changes,
        )


def parse_allowed_users(raw: str) -> set[int]:
    users: set[int] = set()
    for part in raw.split(","):
        value = part.strip()
        if not value:
            continue
        try:
            users.add(int(value))
        except ValueError as exc:
            raise ValueError(f"Invalid TELEGRAM_ALLOWED_USERS entry: {value!r}") from exc
    return users


def parse_bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Invalid boolean value for {name}: {raw!r}")
