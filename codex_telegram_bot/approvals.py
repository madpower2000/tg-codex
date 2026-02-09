from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal

from .state import PendingApprovalState, StateStore

ApprovalDecision = Literal["accept", "decline"]


@dataclass(slots=True)
class PendingApproval:
    request_id: int | str
    method: str
    thread_id: str
    turn_id: str
    params: dict[str, Any]


class ApprovalManager:
    def __init__(
        self,
        send_response: Callable[[int | str, dict[str, Any]], Awaitable[None]],
        state_store: StateStore,
    ) -> None:
        self._send_response = send_response
        self._state_store = state_store
        self._pending_by_chat: dict[int, PendingApproval] = {}
        self._queued_inputs: dict[int, list[str]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def restore_from_state(self) -> None:
        all_states = await self._state_store.all_chat_states()
        async with self._lock:
            for chat_id, chat_state in all_states.items():
                if not chat_state.pending_approval:
                    continue
                pending = chat_state.pending_approval
                self._pending_by_chat[chat_id] = PendingApproval(
                    request_id=pending.request_id,
                    method=pending.method,
                    thread_id=pending.thread_id,
                    turn_id=pending.turn_id,
                    params={},
                )

    async def register_approval(
        self,
        chat_id: int,
        request_id: int | str,
        method: str,
        params: dict[str, Any],
    ) -> PendingApproval:
        thread_id = str(params.get("threadId", ""))
        turn_id = str(params.get("turnId", ""))
        pending = PendingApproval(
            request_id=request_id,
            method=method,
            thread_id=thread_id,
            turn_id=turn_id,
            params=params,
        )
        async with self._lock:
            self._pending_by_chat[chat_id] = pending

        await self._state_store.set_pending_approval(
            chat_id,
            PendingApprovalState(
                request_id=request_id,
                method=method,
                thread_id=thread_id,
                turn_id=turn_id,
            ),
        )
        return pending

    async def has_pending(self, chat_id: int) -> bool:
        async with self._lock:
            return chat_id in self._pending_by_chat

    async def get_pending(self, chat_id: int) -> PendingApproval | None:
        async with self._lock:
            return self._pending_by_chat.get(chat_id)

    async def resolve_approval(self, chat_id: int, decision: ApprovalDecision) -> PendingApproval:
        async with self._lock:
            pending = self._pending_by_chat.pop(chat_id, None)
        if pending is None:
            raise KeyError(f"No pending approval for chat_id={chat_id}")

        await self._send_response(pending.request_id, {"decision": decision})
        await self._state_store.set_pending_approval(chat_id, None)
        return pending

    async def clear_pending(self, chat_id: int) -> None:
        async with self._lock:
            self._pending_by_chat.pop(chat_id, None)
        await self._state_store.set_pending_approval(chat_id, None)

    async def queue_input(self, chat_id: int, text: str) -> None:
        async with self._lock:
            self._queued_inputs[chat_id].append(text)

    async def pop_queued_inputs(self, chat_id: int) -> list[str]:
        async with self._lock:
            queued = list(self._queued_inputs.get(chat_id, []))
            self._queued_inputs[chat_id] = []
            return queued
