from __future__ import annotations

import asyncio
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class PendingApprovalState:
    request_id: int | str
    method: str
    thread_id: str
    turn_id: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PendingApprovalState":
        return cls(
            request_id=data["request_id"],
            method=str(data["method"]),
            thread_id=str(data["thread_id"]),
            turn_id=str(data["turn_id"]),
        )


@dataclass(slots=True)
class ChatState:
    thread_id: str | None = None
    last_turn_id: str | None = None
    pending_approval: PendingApprovalState | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChatState":
        pending_raw = data.get("pending_approval")
        pending = None
        if isinstance(pending_raw, dict):
            pending = PendingApprovalState.from_dict(pending_raw)
        return cls(
            thread_id=str(data["thread_id"]) if data.get("thread_id") else None,
            last_turn_id=str(data["last_turn_id"]) if data.get("last_turn_id") else None,
            pending_approval=pending,
        )


class StateStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._chats: dict[str, ChatState] = {}

    async def load(self) -> None:
        async with self._lock:
            if not self._path.exists():
                self._chats = {}
                return
            raw = self._path.read_text(encoding="utf-8")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                # Corrupted state should not break startup.
                self._chats = {}
                return

            chats = payload.get("chats", {})
            if not isinstance(chats, dict):
                self._chats = {}
                return

            parsed: dict[str, ChatState] = {}
            for chat_id, data in chats.items():
                if not isinstance(data, dict):
                    continue
                parsed[str(chat_id)] = ChatState.from_dict(data)
            self._chats = parsed

    async def save(self) -> None:
        async with self._lock:
            self._save_unlocked()

    async def get_chat_state(self, chat_id: int) -> ChatState:
        async with self._lock:
            state = self._chats.get(str(chat_id))
            if state is None:
                return ChatState()
            pending = state.pending_approval
            return ChatState(
                thread_id=state.thread_id,
                last_turn_id=state.last_turn_id,
                pending_approval=PendingApprovalState(
                    request_id=pending.request_id,
                    method=pending.method,
                    thread_id=pending.thread_id,
                    turn_id=pending.turn_id,
                )
                if pending
                else None,
            )

    async def get_thread_id(self, chat_id: int) -> str | None:
        async with self._lock:
            state = self._chats.get(str(chat_id))
            return state.thread_id if state else None

    async def set_thread_id(self, chat_id: int, thread_id: str) -> None:
        async with self._lock:
            key = str(chat_id)
            state = self._chats.get(key) or ChatState()
            state.thread_id = thread_id
            self._chats[key] = state
            self._save_unlocked()

    async def set_last_turn_id(self, chat_id: int, turn_id: str | None) -> None:
        async with self._lock:
            key = str(chat_id)
            state = self._chats.get(key) or ChatState()
            state.last_turn_id = turn_id
            self._chats[key] = state
            self._save_unlocked()

    async def set_pending_approval(self, chat_id: int, pending: PendingApprovalState | None) -> None:
        async with self._lock:
            key = str(chat_id)
            state = self._chats.get(key) or ChatState()
            state.pending_approval = pending
            self._chats[key] = state
            self._save_unlocked()

    async def reset_chat(self, chat_id: int) -> None:
        async with self._lock:
            self._chats.pop(str(chat_id), None)
            self._save_unlocked()

    async def all_thread_ids(self) -> list[str]:
        async with self._lock:
            thread_ids = {state.thread_id for state in self._chats.values() if state.thread_id}
            return sorted(thread_ids)

    async def chat_id_by_thread_id(self, thread_id: str) -> int | None:
        async with self._lock:
            for chat_id, state in self._chats.items():
                if state.thread_id == thread_id:
                    return int(chat_id)
            return None

    async def all_chat_states(self) -> dict[int, ChatState]:
        async with self._lock:
            return {int(chat_id): state for chat_id, state in self._chats.items()}

    def _save_unlocked(self) -> None:
        payload = {
            "version": 1,
            "chats": {
                chat_id: {
                    "thread_id": state.thread_id,
                    "last_turn_id": state.last_turn_id,
                    "pending_approval": asdict(state.pending_approval) if state.pending_approval else None,
                }
                for chat_id, state in self._chats.items()
            },
        }

        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
        os.replace(tmp_path, self._path)
