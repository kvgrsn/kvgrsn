import json

import httpx
import pytest

from liqmon.rpc.client import RpcClient, RpcError


def make_transport(handlers):
    """handlers: host -> callable(payload) -> (status, body)"""

    def handle(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        status, body = handlers[request.url.host](payload)
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handle)


def ok(payload, result="0x1"):
    if isinstance(payload, list):
        return 200, [{"jsonrpc": "2.0", "id": p["id"], "result": hex(p["id"])} for p in reversed(payload)]
    return 200, {"jsonrpc": "2.0", "id": payload["id"], "result": result}


async def test_failover_on_http_error():
    t = make_transport({"a": lambda p: (503, {}), "b": lambda p: ok(p, "0x2a")})
    c = RpcClient(["http://a", "http://b"], transport=t, backoff_base_s=0.001)
    assert await c.call("eth_blockNumber") == "0x2a"
    await c.close()


async def test_revert_is_raised_immediately_without_failover():
    calls = {"a": 0, "b": 0}

    def revert(p):
        calls["a"] += 1
        return 200, {"jsonrpc": "2.0", "id": p["id"], "error": {"code": 3, "message": "execution reverted", "data": "0x08c379a0"}}

    def never(p):
        calls["b"] += 1
        return ok(p)

    c = RpcClient(["http://a", "http://b"], transport=make_transport({"a": revert, "b": never}), backoff_base_s=0.001)
    with pytest.raises(RpcError) as exc:
        await c.call("eth_call", [{}])
    assert exc.value.is_revert and exc.value.revert_data == "0x08c379a0"
    assert calls == {"a": 1, "b": 0}
    await c.close()


async def test_range_error_classified():
    def limited(p):
        return 200, {"jsonrpc": "2.0", "id": p["id"], "error": {"code": -32602, "message": "eth_getLogs is limited to 0 - 50 blocks range"}}

    c = RpcClient(["http://a"], transport=make_transport({"a": limited}), backoff_base_s=0.001)
    with pytest.raises(RpcError) as exc:
        await c.get_logs({})
    assert exc.value.is_range_error
    await c.close()


async def test_all_endpoints_failing_reports_each_reason():
    def archive(p):
        return 200, {"jsonrpc": "2.0", "id": p["id"], "error": {"code": -32602, "message": "Archive requests require a personal token"}}

    def limit(p):
        return 200, {"jsonrpc": "2.0", "id": p["id"], "error": {"code": -32005, "message": "limit exceeded"}}

    c = RpcClient(["http://a", "http://b"], transport=make_transport({"a": archive, "b": limit}), max_attempts=2, backoff_base_s=0.001)
    with pytest.raises(RpcError) as exc:
        await c.call("eth_getLogs", [{}])
    assert "Archive requests" in exc.value.message and "limit exceeded" in exc.value.message
    await c.close()


async def test_batch_preserves_order_when_response_is_shuffled():
    c = RpcClient(["http://a"], transport=make_transport({"a": ok}), batch_size=3)
    res = await c.batch([("eth_chainId", []) for _ in range(7)])
    # ids are assigned sequentially, results echo the id -> must be increasing
    ints = [int(r, 16) for r in res]
    assert ints == sorted(ints) and len(ints) == 7
    await c.close()
