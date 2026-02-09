import asyncio

from codex_telegram_bot.state import PendingApprovalState, StateStore


def run(coro):
    return asyncio.run(coro)


def test_state_roundtrip(tmp_path):
    state_path = tmp_path / "state.json"
    store = StateStore(state_path)

    run(store.load())
    assert run(store.get_thread_id(123)) is None

    run(store.set_thread_id(123, "thread-abc"))
    run(store.set_last_turn_id(123, "turn-xyz"))
    run(
        store.set_pending_approval(
            123,
            PendingApprovalState(
                request_id=77,
                method="item/commandExecution/requestApproval",
                thread_id="thread-abc",
                turn_id="turn-xyz",
            ),
        )
    )

    assert state_path.exists()
    tmp_file = state_path.with_suffix(state_path.suffix + ".tmp")
    assert not tmp_file.exists()

    store2 = StateStore(state_path)
    run(store2.load())

    chat = run(store2.get_chat_state(123))
    assert chat.thread_id == "thread-abc"
    assert chat.last_turn_id == "turn-xyz"
    assert chat.pending_approval is not None
    assert chat.pending_approval.request_id == 77


def test_reset_chat(tmp_path):
    state_path = tmp_path / "state.json"
    store = StateStore(state_path)

    run(store.load())
    run(store.set_thread_id(1, "thread-1"))
    run(store.reset_chat(1))

    assert run(store.get_thread_id(1)) is None
