from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.error import Conflict, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .approvals import ApprovalDecision, ApprovalManager
from .codex_appserver import CodexAppServerClient, RequestId, RpcResponseError
from .config import Config, PRIMARY_MODEL_DEFAULT
from .rate_limit import RateLimiter
from .state import StateStore

logger = logging.getLogger(__name__)

APPROVAL_METHODS = {
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
}


@dataclass(slots=True)
class BridgeEvent:
    kind: str
    method: str
    params: dict[str, Any]
    request_id: RequestId | None = None


@dataclass(slots=True)
class TurnLiveState:
    chat_id: int
    thread_id: str
    turn_id: str
    live_message_id: int | None
    created_at: float
    output_text: str = ""
    progress: str = ""
    status: str = "inProgress"
    error_message: str | None = None
    dirty: bool = True
    last_rendered: str = ""
    completed: bool = False


class TelegramBridgeBot:
    def __init__(
        self,
        config: Config,
        state_store: StateStore,
        codex_client: CodexAppServerClient,
        approval_manager: ApprovalManager,
    ) -> None:
        self._config = config
        self._state_store = state_store
        self._codex = codex_client
        self._approval_manager = approval_manager

        self._application: Application | None = None
        self._event_queue: asyncio.Queue[BridgeEvent] = asyncio.Queue()
        self._event_worker_task: asyncio.Task[None] | None = None
        self._render_task: asyncio.Task[None] | None = None
        self._edit_limiter = RateLimiter(self._config.live_edit_interval_seconds)

        self._thread_to_chat: dict[str, int] = {}
        self._active_turn_by_chat: dict[int, str] = {}
        self._thread_by_active_turn_id: dict[str, str] = {}
        self._turns_by_key: dict[tuple[str, str], TurnLiveState] = {}

    def run(self) -> None:
        app = (
            Application.builder()
            .token(self._config.telegram_bot_token)
            .post_init(self._post_init)
            .post_shutdown(self._post_shutdown)
            .build()
        )
        app.add_handler(CommandHandler("start", self._cmd_start))
        app.add_handler(CommandHandler("new", self._cmd_new))
        app.add_handler(CommandHandler("reset", self._cmd_reset))
        app.add_handler(CallbackQueryHandler(self._approval_callback, pattern=r"^approval:"))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_text))
        app.add_error_handler(self._on_error)

        self._application = app
        app.run_polling()

    async def _on_error(self, _: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        err = context.error
        if isinstance(err, Conflict):
            logger.error("Telegram polling conflict: another bot instance is running. Shutting down.")
            context.application.stop_running()
            return
        logger.error("Unhandled Telegram error", exc_info=err)

    def _bot(self) -> Bot:
        app = self._application
        if app is None:
            raise RuntimeError("Telegram application is not initialized")
        return app.bot

    async def _post_init(self, _: Application) -> None:
        await self._state_store.load()
        await self._rebuild_thread_index()
        await self._approval_manager.restore_from_state()

        self._codex.add_notification_handler(self._enqueue_notification)
        self._codex.set_server_request_handler(self._enqueue_server_request)
        await self._codex.start()

        self._event_worker_task = asyncio.create_task(self._event_worker(), name="tg-bridge-events")
        self._render_task = asyncio.create_task(self._render_worker(), name="tg-bridge-render")

        if not self._config.telegram_allowed_users:
            logger.warning("No TELEGRAM_ALLOWED_USERS configured; all users will be unauthorized")

    async def _post_shutdown(self, _: Application) -> None:
        for task in (self._event_worker_task, self._render_task):
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        await self._codex.stop()

    async def _rebuild_thread_index(self) -> None:
        states = await self._state_store.all_chat_states()
        self._thread_to_chat = {
            chat_id_state.thread_id: chat_id
            for chat_id, chat_id_state in states.items()
            if chat_id_state.thread_id
        }

    def _enqueue_notification(self, method: str, params: dict[str, Any]) -> None:
        self._event_queue.put_nowait(BridgeEvent(kind="notification", method=method, params=params))

    def _enqueue_server_request(self, request_id: RequestId, method: str, params: dict[str, Any]) -> None:
        self._event_queue.put_nowait(
            BridgeEvent(kind="server_request", method=method, params=params, request_id=request_id)
        )

    async def _event_worker(self) -> None:
        while True:
            event = await self._event_queue.get()
            try:
                if event.kind == "notification":
                    await self._handle_notification(event.method, event.params)
                elif event.kind == "server_request":
                    await self._handle_server_request(event.request_id, event.method, event.params)
            except Exception:
                logger.exception("Failed to process bridge event", extra={"method": event.method})

    async def _render_worker(self) -> None:
        while True:
            await asyncio.sleep(self._config.live_edit_interval_seconds)
            for key, turn in list(self._turns_by_key.items()):
                if not turn.dirty:
                    continue
                if turn.chat_id <= 0:
                    if turn.completed:
                        self._turns_by_key.pop(key, None)
                    continue

                try:
                    rendered = self._compose_turn_message(turn)
                    if rendered == turn.last_rendered:
                        turn.dirty = False
                        continue

                    bot = self._bot()
                    if turn.live_message_id is not None:
                        await self._edit_limiter.wait_turn()
                        await bot.edit_message_text(
                            chat_id=turn.chat_id,
                            message_id=turn.live_message_id,
                            text=rendered,
                        )
                    else:
                        sent = await bot.send_message(chat_id=turn.chat_id, text=rendered)
                        turn.live_message_id = sent.message_id

                    turn.last_rendered = rendered
                    turn.dirty = False
                except TelegramError:
                    logger.exception(
                        "Telegram edit failed",
                        extra={"chat_id": turn.chat_id, "thread_id": turn.thread_id, "turn_id": turn.turn_id},
                    )
                    if turn.completed:
                        self._turns_by_key.pop(key, None)
                        self._thread_by_active_turn_id.pop(turn.turn_id, None)
                        if self._active_turn_by_chat.get(turn.chat_id) == turn.turn_id:
                            self._active_turn_by_chat.pop(turn.chat_id, None)
                        await self._drain_queued_inputs(turn.chat_id)
                    continue

                if turn.completed:
                    self._turns_by_key.pop(key, None)
                    self._thread_by_active_turn_id.pop(turn.turn_id, None)
                    if self._active_turn_by_chat.get(turn.chat_id) == turn.turn_id:
                        self._active_turn_by_chat.pop(turn.chat_id, None)
                    await self._drain_queued_inputs(turn.chat_id)

    async def _cmd_start(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorize_update(update):
            return
        message = update.effective_message
        if message is None:
            return

        text = (
            "Telegram to Codex bridge.\n"
            "Commands:\n"
            "/new - create a new Codex thread for this chat\n"
            "/reset - forget this chat's thread mapping\n"
            "Any non-command message starts a new turn in the mapped thread."
        )
        await message.reply_text(text)

    async def _cmd_new(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorize_update(update):
            return
        message = update.effective_message
        chat = update.effective_chat
        if message is None or chat is None:
            return

        chat_id = chat.id
        try:
            thread_id = await self._create_new_thread(chat_id)
            await message.reply_text(f"New Codex thread created: {thread_id}")
        except Exception:
            logger.exception("Failed to create new thread", extra={"chat_id": chat_id})
            await message.reply_text("Failed to create new thread.")

    async def _cmd_reset(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorize_update(update):
            return
        message = update.effective_message
        chat = update.effective_chat
        if message is None or chat is None:
            return

        chat_id = chat.id
        state = await self._state_store.get_chat_state(chat_id)
        if state.thread_id:
            self._thread_to_chat.pop(state.thread_id, None)

        await self._state_store.reset_chat(chat_id)
        await self._approval_manager.clear_pending(chat_id)
        active_turn_id = self._active_turn_by_chat.pop(chat_id, None)
        if active_turn_id:
            self._thread_by_active_turn_id.pop(active_turn_id, None)
        self._turns_by_key = {key: value for key, value in self._turns_by_key.items() if value.chat_id != chat_id}
        await message.reply_text("Thread mapping reset for this chat.")

    async def _on_text(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorize_update(update):
            return

        message = update.effective_message
        if message is None or not message.text:
            return

        chat_id = message.chat_id
        text = message.text.strip()
        if not text:
            return

        if await self._approval_manager.has_pending(chat_id):
            await self._approval_manager.queue_input(chat_id, text)
            await message.reply_text("Approval pending. I queued your message.")
            return

        if chat_id in self._active_turn_by_chat:
            await self._approval_manager.queue_input(chat_id, text)
            await message.reply_text("Turn in progress. I queued your message.")
            return

        try:
            await self._start_turn(chat_id, text, reply_to_message=message)
        except Exception:
            logger.exception("Failed to start turn", extra={"chat_id": chat_id})
            await message.reply_text("Failed to start turn.")

    async def _approval_callback(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None:
            return

        if not await self._authorize_callback(update):
            return

        data = query.data or ""
        parts = data.split(":", 3)
        if len(parts) != 4:
            await query.answer("Invalid approval payload", show_alert=True)
            return

        _, chat_id_raw, request_id_raw, decision_raw = parts
        if decision_raw == "accept":
            decision: ApprovalDecision = "accept"
        elif decision_raw == "decline":
            decision = "decline"
        else:
            await query.answer("Invalid decision", show_alert=True)
            return

        try:
            chat_id = int(chat_id_raw)
        except ValueError:
            await query.answer("Invalid chat id", show_alert=True)
            return

        chat = update.effective_chat
        if chat is None or chat.id != chat_id:
            await query.answer("Approval does not belong to this chat", show_alert=True)
            return

        pending = await self._approval_manager.get_pending(chat_id)
        if pending is None:
            await query.answer("No pending approval", show_alert=True)
            return
        if str(pending.request_id) != request_id_raw:
            await query.answer("Approval request expired", show_alert=True)
            return

        try:
            await self._approval_manager.resolve_approval(chat_id, decision)
        except Exception:
            logger.exception("Failed to resolve approval", extra={"chat_id": chat_id})
            await query.answer("Failed to send approval decision", show_alert=True)
            return
        await query.answer(f"Decision sent: {decision}")

        message_text = getattr(query.message, "text", None)
        original = message_text if isinstance(message_text, str) and message_text else "Approval"
        await query.edit_message_text(f"{original}\n\nDecision: {decision}")
        await self._drain_queued_inputs(chat_id)

    async def _start_turn(self, chat_id: int, text: str, reply_to_message: Message | None = None) -> None:
        thread_id = await self._ensure_thread(chat_id)

        response = await self._codex.send_request(
            method="turn/start",
            params={
                "threadId": thread_id,
                "input": [{"type": "text", "text": text}],
                "cwd": str(self._config.codex_cwd),
            },
        )

        turn_payload = response.get("turn")
        if not isinstance(turn_payload, dict) or "id" not in turn_payload:
            raise RuntimeError(f"Invalid turn/start response: {response!r}")
        turn_id = str(turn_payload["id"])
        self._active_turn_by_chat[chat_id] = turn_id
        self._thread_by_active_turn_id[turn_id] = thread_id
        await self._state_store.set_last_turn_id(chat_id, turn_id)

        live = self._get_or_create_turn_state(thread_id=thread_id, turn_id=turn_id)
        if live.chat_id != chat_id:
            live.chat_id = chat_id
        live.dirty = True

        if live.live_message_id is None:
            if reply_to_message is not None:
                msg = await reply_to_message.reply_text("Working on it...")
            else:
                msg = await self._bot().send_message(chat_id=chat_id, text="Working on it...")
            live.live_message_id = msg.message_id

        logger.info(
            "Started turn",
            extra={"chat_id": chat_id, "thread_id": thread_id, "turn_id": turn_id},
        )

    async def _ensure_thread(self, chat_id: int) -> str:
        thread_id = await self._state_store.get_thread_id(chat_id)
        if thread_id:
            return thread_id
        return await self._create_new_thread(chat_id)

    async def _create_new_thread(self, chat_id: int) -> str:
        existing_thread_id = await self._state_store.get_thread_id(chat_id)

        models = [self._config.codex_model]
        if (
            self._config.codex_model == PRIMARY_MODEL_DEFAULT
            and self._config.codex_fallback_model not in models
        ):
            models.append(self._config.codex_fallback_model)

        last_error: Exception | None = None
        for model in models:
            try:
                response = await self._codex.send_request("thread/start", {"model": model})
                thread_payload = response.get("thread")
                if not isinstance(thread_payload, dict) or "id" not in thread_payload:
                    raise RuntimeError(f"Invalid thread/start response: {response!r}")
                thread_id = str(thread_payload["id"])

                if existing_thread_id:
                    self._thread_to_chat.pop(existing_thread_id, None)
                await self._state_store.set_thread_id(chat_id, thread_id)
                await self._state_store.set_last_turn_id(chat_id, None)
                self._thread_to_chat[thread_id] = chat_id
                logger.info(
                    "Created thread",
                    extra={"chat_id": chat_id, "thread_id": thread_id},
                )
                return thread_id
            except RpcResponseError as exc:
                last_error = exc
                logger.warning(
                    "thread/start failed",
                    extra={"chat_id": chat_id, "method": "thread/start"},
                )

        raise RuntimeError(f"Failed to create thread: {last_error!r}")

    async def _handle_notification(self, method: str, params: dict[str, Any]) -> None:
        if method.startswith("codex/event/"):
            await self._handle_codex_event_notification(params)
            return

        if method == "item/agentMessage/delta":
            thread_id = str(params.get("threadId", ""))
            turn_id = str(params.get("turnId", ""))
            if not thread_id or not turn_id:
                return
            turn = self._get_or_create_turn_state(thread_id=thread_id, turn_id=turn_id)
            delta = str(params.get("delta", ""))
            if delta:
                turn.output_text += delta
                turn.dirty = True
            return

        if method in {"item/started", "item/completed"}:
            thread_id = str(params.get("threadId", ""))
            turn_id = str(params.get("turnId", ""))
            if not thread_id or not turn_id:
                return
            turn = self._get_or_create_turn_state(thread_id=thread_id, turn_id=turn_id)
            raw_item = params.get("item")
            item: dict[str, Any] = raw_item if isinstance(raw_item, dict) else {}
            item_type = str(item.get("type", "item"))
            phase = "started" if method.endswith("started") else "completed"
            turn.progress = f"{item_type} {phase}"
            if method == "item/completed":
                maybe_agent_text = self._extract_agent_text_from_item(item)
                self._merge_agent_text(turn, maybe_agent_text)
            turn.dirty = True
            return

        if method in {"item/commandExecution/outputDelta", "item/fileChange/outputDelta"}:
            thread_id = str(params.get("threadId", ""))
            turn_id = str(params.get("turnId", ""))
            if not thread_id or not turn_id:
                return
            turn = self._get_or_create_turn_state(thread_id=thread_id, turn_id=turn_id)
            if method.startswith("item/commandExecution"):
                turn.progress = "command output streaming"
            else:
                turn.progress = "file change output streaming"
            turn.dirty = True
            return

        if method == "turn/started":
            thread_id = str(params.get("threadId", ""))
            raw_turn = params.get("turn")
            turn_payload: dict[str, Any] = raw_turn if isinstance(raw_turn, dict) else {}
            turn_id = str(turn_payload.get("id", ""))
            if not turn_id:
                return
            resolved_thread_id = self._resolve_thread_id_for_turn(thread_id=thread_id, turn_id=turn_id)
            if not resolved_thread_id:
                return
            self._thread_by_active_turn_id[turn_id] = resolved_thread_id
            self._get_or_create_turn_state(thread_id=resolved_thread_id, turn_id=turn_id)

            chat_id = self._thread_to_chat.get(resolved_thread_id)
            if chat_id is not None:
                await self._state_store.set_last_turn_id(chat_id, turn_id)
            return

        if method == "turn/completed":
            thread_id = str(params.get("threadId", ""))
            raw_turn = params.get("turn")
            turn_payload: dict[str, Any] = raw_turn if isinstance(raw_turn, dict) else {}
            turn_id = str(turn_payload.get("id", ""))
            if not turn_id:
                return

            resolved_thread_id = self._resolve_thread_id_for_turn(thread_id=thread_id, turn_id=turn_id)
            if not resolved_thread_id:
                logger.warning("turn/completed missing thread id and no active mapping", extra={"turn_id": turn_id})
                return

            turn = self._get_or_create_turn_state(thread_id=resolved_thread_id, turn_id=turn_id)

            turn.status = str(turn_payload.get("status", "completed"))
            error_payload = turn_payload.get("error")
            if isinstance(error_payload, dict):
                message = error_payload.get("message")
                if message:
                    turn.error_message = str(message)

            turn.completed = True
            turn.dirty = True

            logger.info(
                "Turn completed",
                extra={"chat_id": turn.chat_id, "thread_id": turn.thread_id, "turn_id": turn_id},
            )

    async def _handle_codex_event_notification(self, params: dict[str, Any]) -> None:
        raw_msg = params.get("msg")
        msg: dict[str, Any] = raw_msg if isinstance(raw_msg, dict) else {}
        event_type = str(msg.get("type", ""))

        thread_id, turn_id = self._resolve_event_ids(params, msg)
        if not thread_id or not turn_id:
            return

        turn = self._get_or_create_turn_state(thread_id=thread_id, turn_id=turn_id)

        if event_type == "agent_message_delta":
            delta = msg.get("delta")
            if isinstance(delta, str) and delta:
                turn.output_text += delta
                turn.dirty = True
            return

        if event_type == "agent_message":
            message = msg.get("message")
            if isinstance(message, str) and message:
                self._merge_agent_text(turn, message.strip())
                turn.progress = "agent message completed"
                turn.dirty = True
            return

        if event_type in {"item_started", "item_completed"}:
            raw_item = msg.get("item")
            item: dict[str, Any] = raw_item if isinstance(raw_item, dict) else {}
            item_type = str(item.get("type", "item")).lower()
            phase = "started" if event_type == "item_started" else "completed"
            turn.progress = f"{item_type} {phase}"
            if event_type == "item_completed":
                maybe_agent_text = self._extract_agent_text_from_item(item)
                self._merge_agent_text(turn, maybe_agent_text)
            turn.dirty = True
            return

        if event_type in {"task_complete", "task_completed"}:
            turn.status = str(msg.get("status", "completed"))
            raw_error = msg.get("error")
            if isinstance(raw_error, str) and raw_error:
                turn.error_message = raw_error
            elif isinstance(raw_error, dict):
                detail = raw_error.get("message")
                if isinstance(detail, str) and detail:
                    turn.error_message = detail
            turn.completed = True
            turn.dirty = True
            return

    def _get_or_create_turn_state(self, thread_id: str, turn_id: str) -> TurnLiveState:
        key = (thread_id, turn_id)
        existing = self._turns_by_key.get(key)
        if existing is not None:
            return existing

        chat_id = self._thread_to_chat.get(thread_id)
        if chat_id is None:
            chat_id = 0

        created = TurnLiveState(
            chat_id=chat_id,
            thread_id=thread_id,
            turn_id=turn_id,
            live_message_id=None,
            created_at=time.monotonic(),
        )
        self._turns_by_key[key] = created
        return created

    def _resolve_thread_id_for_turn(self, thread_id: str, turn_id: str) -> str:
        if thread_id:
            return thread_id

        mapped = self._thread_by_active_turn_id.get(turn_id)
        if mapped:
            return mapped

        candidates = [existing_thread_id for existing_thread_id, existing_turn_id in self._turns_by_key if existing_turn_id == turn_id]
        if len(candidates) == 1:
            return candidates[0]
        return ""

    @staticmethod
    def _resolve_event_ids(params: dict[str, Any], msg: dict[str, Any]) -> tuple[str, str]:
        thread_id = ""
        turn_id = ""

        msg_thread_id = msg.get("thread_id")
        msg_turn_id = msg.get("turn_id")
        if isinstance(msg_thread_id, str) and msg_thread_id:
            thread_id = msg_thread_id
        if isinstance(msg_turn_id, str) and msg_turn_id:
            turn_id = msg_turn_id

        if not thread_id:
            conversation_id = params.get("conversationId")
            if isinstance(conversation_id, str):
                thread_id = conversation_id
        if not turn_id:
            event_id = params.get("id")
            if isinstance(event_id, str):
                turn_id = event_id
            elif isinstance(event_id, int):
                turn_id = str(event_id)

        return thread_id, turn_id

    @staticmethod
    def _extract_agent_text_from_item(item: dict[str, Any]) -> str:
        item_type = str(item.get("type", "")).lower()
        if item_type not in {"agentmessage", "agent_message"}:
            return ""

        chunks: list[str] = []
        direct_text = item.get("text")
        if isinstance(direct_text, str) and direct_text:
            chunks.append(direct_text)

        content = item.get("content")
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                text = part.get("text")
                if isinstance(text, str) and text:
                    chunks.append(text)

        return "".join(chunks).strip()

    @staticmethod
    def _merge_agent_text(turn: TurnLiveState, text: str) -> None:
        if not text:
            return
        if not turn.output_text:
            turn.output_text = text
            return
        if text.startswith(turn.output_text):
            turn.output_text = text

    async def _handle_server_request(
        self,
        request_id: RequestId | None,
        method: str,
        params: dict[str, Any],
    ) -> None:
        if request_id is None:
            return

        thread_id = str(params.get("threadId", ""))
        chat_id = self._thread_to_chat.get(thread_id)
        if chat_id is None:
            chat_id = await self._state_store.chat_id_by_thread_id(thread_id)
            if chat_id is not None:
                self._thread_to_chat[thread_id] = chat_id

        if method == "item/tool/requestUserInput":
            await self._codex.send_server_request_response(request_id, {"answers": []})
            logger.warning(
                "Auto-answered tool user-input request with empty answers",
                extra={"thread_id": thread_id, "request_id": request_id, "method": method},
            )
            return

        if method not in APPROVAL_METHODS:
            await self._codex.send_server_request_error(
                request_id=request_id,
                code=-32601,
                message=f"Unsupported server request method: {method}",
            )
            logger.warning(
                "Rejected unsupported server request",
                extra={"thread_id": thread_id, "request_id": request_id, "method": method},
            )
            return

        if chat_id is None:
            await self._codex.send_server_request_response(request_id, {"decision": "decline"})
            logger.warning(
                "Declined approval because thread has no chat mapping",
                extra={"thread_id": thread_id, "request_id": request_id, "method": method},
            )
            return

        pending = await self._approval_manager.register_approval(chat_id, request_id, method, params)
        await self._bot().send_message(
            chat_id=chat_id,
            text=self._format_approval_text(method, params),
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Accept",
                            callback_data=f"approval:{chat_id}:{pending.request_id}:accept",
                        ),
                        InlineKeyboardButton(
                            "Decline",
                            callback_data=f"approval:{chat_id}:{pending.request_id}:decline",
                        ),
                    ]
                ]
            ),
        )

        turn = self._resolve_turn(params)
        if turn is not None:
            turn.progress = "Awaiting approval"
            turn.dirty = True

    def _format_approval_text(self, method: str, params: dict[str, Any]) -> str:
        thread_id = str(params.get("threadId", ""))
        turn_id = str(params.get("turnId", ""))
        reason = str(params.get("reason", "")).strip()

        lines = ["Approval required", f"Method: {method}", f"Thread: {thread_id}", f"Turn: {turn_id}"]
        if method == "item/commandExecution/requestApproval":
            command = str(params.get("command", "")).strip()
            cwd = str(params.get("cwd", "")).strip()
            if command:
                lines.append(f"Command: {command}")
            if cwd:
                lines.append(f"Cwd: {cwd}")
        elif method == "item/fileChange/requestApproval":
            grant_root = str(params.get("grantRoot", "")).strip()
            if grant_root:
                lines.append(f"Scope: {grant_root}")

        if reason:
            lines.append(f"Reason: {reason}")
        return "\n".join(lines)

    async def _drain_queued_inputs(self, chat_id: int) -> None:
        if await self._approval_manager.has_pending(chat_id):
            return
        if chat_id in self._active_turn_by_chat:
            return

        queued = await self._approval_manager.pop_queued_inputs(chat_id)
        for text in queued:
            if await self._approval_manager.has_pending(chat_id):
                await self._approval_manager.queue_input(chat_id, text)
                return
            if chat_id in self._active_turn_by_chat:
                await self._approval_manager.queue_input(chat_id, text)
                return
            await self._start_turn(chat_id, text)

    def _resolve_turn(self, params: dict[str, Any]) -> TurnLiveState | None:
        thread_id = str(params.get("threadId", ""))
        turn_id = str(params.get("turnId", ""))

        if not turn_id:
            turn_payload = params.get("turn")
            if isinstance(turn_payload, dict):
                turn_id = str(turn_payload.get("id", ""))

        if not thread_id or not turn_id:
            return None

        return self._turns_by_key.get((thread_id, turn_id))

    def _compose_turn_message(self, turn: TurnLiveState) -> str:
        status = turn.status
        if turn.completed:
            header = f"Status: {status}"
        else:
            header = "Status: running"

        lines = [header]
        if turn.progress:
            lines.append(f"Progress: {turn.progress}")

        body = turn.output_text.strip()
        if not body:
            body = "(no agent message yet)"

        if len(body) > 3600:
            body = "...[truncated]\n" + body[-3600:]
        lines.append("")
        lines.append(body)

        if turn.error_message:
            lines.append("")
            lines.append(f"Error: {turn.error_message}")

        return "\n".join(lines)

    async def _authorize_update(self, update: Update) -> bool:
        user = update.effective_user
        if user is None or user.id not in self._config.telegram_allowed_users:
            if update.effective_message:
                await update.effective_message.reply_text("unauthorized")
            return False
        return True

    async def _authorize_callback(self, update: Update) -> bool:
        query = update.callback_query
        user = update.effective_user
        if query is None:
            return False
        if user is None or user.id not in self._config.telegram_allowed_users:
            await query.answer("unauthorized", show_alert=True)
            return False
        return True
