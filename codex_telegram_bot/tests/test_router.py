import asyncio

import pytest

from codex_telegram_bot.codex_appserver import RpcResponseError, RpcRouter


def test_router_routes_response_by_id():
    async def scenario() -> None:
        router = RpcRouter()
        future = router.register(10)

        route = router.route_message({"id": 10, "result": {"ok": True}})

        assert route == "response"
        assert await future == {"ok": True}

    asyncio.run(scenario())


def test_router_routes_error_response_by_id():
    async def scenario() -> None:
        router = RpcRouter()
        future = router.register("req-1")

        route = router.route_message({"id": "req-1", "error": {"code": -32001, "message": "bad"}})

        assert route == "response"
        with pytest.raises(RpcResponseError):
            await future

    asyncio.run(scenario())


def test_router_classifies_server_requests_and_notifications():
    router = RpcRouter()

    assert router.route_message({"id": 9, "method": "item/fileChange/requestApproval", "params": {}}) == "server_request"
    assert router.route_message({"method": "turn/completed", "params": {}}) == "notification"
