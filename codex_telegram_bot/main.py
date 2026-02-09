from __future__ import annotations

import asyncio
import logging

from .approvals import ApprovalManager
from .codex_appserver import CodexAppServerClient
from .config import Config
from .logging_setup import setup_logging
from .state import StateStore
from .telegram_bot import TelegramBridgeBot


def main() -> None:
    config = Config.from_env()
    setup_logging(config.log_level)

    state_store = StateStore(config.state_path)
    asyncio.run(state_store.load())

    codex_client = CodexAppServerClient(
        cwd=config.codex_cwd,
        known_thread_ids_provider=state_store.all_thread_ids,
    )
    approval_manager = ApprovalManager(
        send_response=codex_client.send_server_request_response,
        state_store=state_store,
    )

    bot = TelegramBridgeBot(
        config=config,
        state_store=state_store,
        codex_client=codex_client,
        approval_manager=approval_manager,
    )

    logging.getLogger(__name__).info("Starting Telegram bridge")
    bot.run()


if __name__ == "__main__":
    main()
