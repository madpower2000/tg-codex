import asyncio

from codex_telegram_bot.approvals import ApprovalManager
from codex_telegram_bot.state import StateStore


def run(coro):
    return asyncio.run(coro)


def test_approval_flow_state_machine(tmp_path):
    state_path = tmp_path / "state.json"
    store = StateStore(state_path)
    run(store.load())

    sent_responses: list[tuple[int | str, dict[str, str]]] = []

    async def send_response(request_id: int | str, result: dict[str, str]) -> None:
        sent_responses.append((request_id, result))

    manager = ApprovalManager(send_response=send_response, state_store=store)

    pending = run(
        manager.register_approval(
            chat_id=99,
            request_id=42,
            method="item/commandExecution/requestApproval",
            params={"threadId": "thread-a", "turnId": "turn-b", "command": "echo hi"},
        )
    )

    assert pending.request_id == 42
    assert run(manager.has_pending(99))

    run(manager.queue_input(99, "queued message"))
    run(manager.resolve_approval(99, "accept"))

    assert sent_responses == [(42, {"decision": "accept"})]
    assert not run(manager.has_pending(99))
    assert run(manager.pop_queued_inputs(99)) == ["queued message"]

    chat = run(store.get_chat_state(99))
    assert chat.pending_approval is None
