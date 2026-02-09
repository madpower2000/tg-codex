from __future__ import annotations

import asyncio
import inspect
import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

RequestId = int | str

NotificationHandler = Callable[[str, dict[str, Any]], Awaitable[None] | None]
ServerRequestHandler = Callable[[RequestId, str, dict[str, Any]], Awaitable[None] | None]


class RpcResponseError(Exception):
    def __init__(self, request_id: RequestId, error: Any) -> None:
        self.request_id = request_id
        self.error = error
        super().__init__(f"RPC request {request_id!r} failed: {error!r}")


class RpcRouter:
    def __init__(self) -> None:
        self._pending: dict[RequestId, asyncio.Future[Any]] = {}

    def register(self, request_id: RequestId) -> asyncio.Future[Any]:
        if request_id in self._pending:
            raise RuntimeError(f"Duplicate request id: {request_id!r}")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        self._pending[request_id] = fut
        return fut

    def discard(self, request_id: RequestId) -> None:
        self._pending.pop(request_id, None)

    def fail_all(self, exc: Exception) -> None:
        pending = list(self._pending.values())
        self._pending.clear()
        for fut in pending:
            if not fut.done():
                fut.set_exception(exc)

    def route_message(self, message: dict[str, Any]) -> str:
        has_id = "id" in message
        has_method = "method" in message

        if has_id and not has_method:
            request_id = message["id"]
            fut = self._pending.pop(request_id, None)
            if fut is None:
                return "orphan_response"
            if "error" in message:
                fut.set_exception(RpcResponseError(request_id=request_id, error=message["error"]))
            else:
                fut.set_result(message.get("result"))
            return "response"

        if has_id and has_method:
            return "server_request"

        if has_method:
            return "notification"

        return "unknown"


@dataclass(slots=True)
class InitializeClientInfo:
    name: str = "telegram_bridge"
    title: str = "Telegram Bridge"
    version: str = "0.1.0"


class CodexAppServerClient:
    def __init__(
        self,
        cwd: Path,
        known_thread_ids_provider: Callable[[], Awaitable[list[str]]] | None = None,
    ) -> None:
        self._cwd = cwd
        self._known_thread_ids_provider = known_thread_ids_provider

        self._process: subprocess.Popen[str] | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._restart_task: asyncio.Task[None] | None = None
        self._writer_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()

        self._router = RpcRouter()
        self._ready_event = asyncio.Event()
        self._stopping = False
        self._next_request_id = 1

        self._notification_handlers: list[NotificationHandler] = []
        self._server_request_handler: ServerRequestHandler | None = None

    def add_notification_handler(self, handler: NotificationHandler) -> None:
        self._notification_handlers.append(handler)

    def set_server_request_handler(self, handler: ServerRequestHandler) -> None:
        self._server_request_handler = handler

    async def start(self) -> None:
        self._stopping = False
        await self._restart("initial start")

    async def stop(self) -> None:
        self._stopping = True
        self._ready_event.clear()

        if self._restart_task and not self._restart_task.done():
            self._restart_task.cancel()
            await asyncio.gather(self._restart_task, return_exceptions=True)

        await self._terminate_process()
        self._router.fail_all(RuntimeError("Codex app-server stopped"))

    async def send_request(
        self,
        method: str,
        params: dict[str, Any],
        timeout_seconds: float = 120.0,
    ) -> Any:
        await self._ready_event.wait()
        request_id = self._next_request_id
        self._next_request_id += 1
        return await self._send_request_with_id(request_id, method, params, timeout_seconds)

    async def send_notification(self, method: str, params: dict[str, Any]) -> None:
        await self._ready_event.wait()
        await self._write_message({"method": method, "params": params})

    async def send_server_request_response(self, request_id: RequestId, result: dict[str, Any]) -> None:
        await self._ready_event.wait()
        await self._write_message({"id": request_id, "result": result})

    async def _send_request_with_id(
        self,
        request_id: RequestId,
        method: str,
        params: dict[str, Any],
        timeout_seconds: float,
    ) -> Any:
        future = self._router.register(request_id)
        try:
            await self._write_message({"method": method, "id": request_id, "params": params})
        except Exception:
            self._router.discard(request_id)
            raise

        try:
            return await asyncio.wait_for(future, timeout=timeout_seconds)
        except Exception:
            self._router.discard(request_id)
            raise

    async def _restart(self, reason: str) -> None:
        async with self._lifecycle_lock:
            if self._stopping:
                return

            logger.warning("Restarting codex app-server", extra={"method": reason})
            self._ready_event.clear()
            self._router.fail_all(ConnectionError("codex app-server restarting"))
            await self._terminate_process()

            backoff = 1.0
            while not self._stopping:
                try:
                    await self._spawn_and_initialize()
                    logger.info("codex app-server ready")
                    self._ready_event.set()
                    return
                except Exception:
                    logger.exception("Failed to start codex app-server")
                    await self._terminate_process()
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 10.0)

    async def _spawn_and_initialize(self) -> None:
        self._process = subprocess.Popen(
            ["codex", "app-server"],
            cwd=str(self._cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        if self._process.stdin is None or self._process.stdout is None:
            raise RuntimeError("codex app-server did not provide stdio pipes")

        self._reader_task = asyncio.create_task(self._reader_loop(), name="codex-appserver-reader")

        init_params = {
            "clientInfo": {
                "name": InitializeClientInfo.name,
                "title": InitializeClientInfo.title,
                "version": InitializeClientInfo.version,
            }
        }
        await self._send_request_with_id(0, "initialize", init_params, timeout_seconds=15.0)
        await self._write_message({"method": "initialized", "params": {}})
        await self._resume_known_threads()

    async def _resume_known_threads(self) -> None:
        if self._known_thread_ids_provider is None:
            return

        thread_ids = await self._known_thread_ids_provider()
        for thread_id in thread_ids:
            request_id = self._next_request_id
            self._next_request_id += 1
            try:
                await self._send_request_with_id(
                    request_id,
                    "thread/resume",
                    {"threadId": thread_id},
                    timeout_seconds=20.0,
                )
                logger.info("Resumed thread", extra={"thread_id": thread_id})
            except Exception:
                logger.exception("Failed to resume thread", extra={"thread_id": thread_id})

    async def _reader_loop(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return

        try:
            while not self._stopping:
                line = await asyncio.to_thread(process.stdout.readline)
                if line == "":
                    if process.poll() is not None:
                        break
                    await asyncio.sleep(0.05)
                    continue

                raw = line.strip()
                if not raw:
                    continue

                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("Skipping invalid app-server JSON line")
                    continue

                if not isinstance(message, dict):
                    logger.warning("Skipping non-object app-server message")
                    continue

                route = self._router.route_message(message)
                if route == "notification":
                    self._dispatch_notification(message)
                elif route == "server_request":
                    self._dispatch_server_request(message)
                elif route == "unknown":
                    logger.warning("Unrecognized app-server message shape")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Reader loop crashed")
        finally:
            if not self._stopping and self._process is process:
                self._schedule_restart("reader ended")

    def _dispatch_notification(self, message: dict[str, Any]) -> None:
        method = str(message.get("method", ""))
        params = message.get("params", {})
        if not isinstance(params, dict):
            params = {}

        for handler in self._notification_handlers:
            try:
                result = handler(method, params)
                if inspect.isawaitable(result):
                    task = asyncio.create_task(result)
                    task.add_done_callback(self._log_task_error)
            except Exception:
                logger.exception("Notification handler failed", extra={"method": method})

    def _dispatch_server_request(self, message: dict[str, Any]) -> None:
        method = str(message.get("method", ""))
        request_id = message.get("id")
        params = message.get("params", {})
        if not isinstance(params, dict):
            params = {}

        handler = self._server_request_handler
        if handler is None:
            logger.error(
                "Server request received but no handler configured",
                extra={"method": method, "request_id": request_id},
            )
            return

        try:
            result = handler(request_id, method, params)
            if inspect.isawaitable(result):
                task = asyncio.create_task(result)
                task.add_done_callback(self._log_task_error)
        except Exception:
            logger.exception(
                "Server request handler failed",
                extra={"method": method, "request_id": request_id},
            )

    def _schedule_restart(self, reason: str) -> None:
        if self._stopping:
            return
        if self._restart_task and not self._restart_task.done():
            return
        self._restart_task = asyncio.create_task(self._restart(reason), name="codex-appserver-restart")

    async def _terminate_process(self) -> None:
        process = self._process
        self._process = None

        reader_task = self._reader_task
        self._reader_task = None
        if reader_task and not reader_task.done() and reader_task is not asyncio.current_task():
            reader_task.cancel()
            await asyncio.gather(reader_task, return_exceptions=True)

        if process is None:
            return

        await asyncio.to_thread(self._terminate_process_blocking, process)

    async def _write_message(self, message: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            self._schedule_restart("write with no process")
            raise ConnectionError("codex app-server stdin is unavailable")

        payload = json.dumps(message, ensure_ascii=True, separators=(",", ":")) + "\n"
        async with self._writer_lock:
            await asyncio.to_thread(self._write_blocking, process, payload)

    @staticmethod
    def _write_blocking(process: subprocess.Popen[str], payload: str) -> None:
        assert process.stdin is not None
        process.stdin.write(payload)
        process.stdin.flush()

    @staticmethod
    def _terminate_process_blocking(process: subprocess.Popen[str]) -> None:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    @staticmethod
    def _log_task_error(task: asyncio.Task[Any]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.exception("Background task failed", exc_info=exc)
