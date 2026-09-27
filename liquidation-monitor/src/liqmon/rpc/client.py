"""Async JSON-RPC client with batching, rate limiting, retries and failover.

Design notes
------------
* One ``RpcClient`` per chain. It owns an ordered list of endpoints; the first
  healthy endpoint is used, and failing endpoints are put on a cooldown.
* Transport failures, HTTP 429/5xx and non-deterministic JSON-RPC errors
  (rate limits, "header not found", pruned state...) are retried on the next
  endpoint with exponential backoff.
* Deterministic errors are raised immediately: execution reverts (the call
  itself is wrong, retrying elsewhere cannot help) and log-range errors (the
  caller must split the range).
* Each endpoint has a token-bucket limiter; a batch costs one token per item.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import random
import time
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

import httpx

log = logging.getLogger(__name__)

_RANGE_ERROR_HINTS = (
    "block range",
    "range is too large",
    "range too large",
    "query returned more than",
    "exceed maximum block range",
    "too many blocks",
    "logs matched by query exceeds",
    "maximum is set to",
    "response size exceeded",
    "query timeout exceeded",
    "is limited to",
)

_TRANSIENT_HTTP = {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 524}


class RpcTransportError(Exception):
    """Network-level failure (connection, timeout, bad HTTP status)."""


class RpcError(Exception):
    def __init__(self, code: int | None, message: str, data: Any = None, endpoint: str | None = None):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message or ""
        self.data = data
        self.endpoint = endpoint

    @property
    def is_revert(self) -> bool:
        msg = self.message.lower()
        return self.code == 3 or "revert" in msg

    @property
    def is_range_error(self) -> bool:
        msg = self.message.lower()
        return any(h in msg for h in _RANGE_ERROR_HINTS)

    @property
    def revert_data(self) -> str | None:
        if isinstance(self.data, str) and self.data.startswith("0x"):
            return self.data
        if isinstance(self.data, dict):
            d = self.data.get("data")
            if isinstance(d, str) and d.startswith("0x"):
                return d
        return None

    @property
    def deterministic(self) -> bool:
        return self.is_revert or self.is_range_error


class TokenBucket:
    def __init__(self, rate: float, capacity: float | None = None):
        self.rate = max(rate, 0.1)
        self.capacity = capacity if capacity is not None else max(self.rate, 1.0)
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, cost: float = 1.0) -> None:
        cost = min(cost, self.capacity)
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= cost:
                    self._tokens -= cost
                    return
                await asyncio.sleep((cost - self._tokens) / self.rate)


@dataclass
class _Endpoint:
    url: str
    limiter: TokenBucket
    failures: int = 0
    cooldown_until: float = 0.0

    @property
    def healthy(self) -> bool:
        return time.monotonic() >= self.cooldown_until

    def mark_failure(self) -> None:
        self.failures += 1
        self.cooldown_until = time.monotonic() + min(60.0, 2.0 ** min(self.failures, 6))

    def mark_success(self) -> None:
        self.failures = 0
        self.cooldown_until = 0.0

    @property
    def label(self) -> str:
        # Never log full URLs: they may embed API keys.
        return self.url.split("//", 1)[-1].split("/", 1)[0]


class RpcClient:
    def __init__(
        self,
        urls: Sequence[str],
        *,
        requests_per_second: float = 10.0,
        timeout_s: float = 30.0,
        max_attempts: int = 5,
        backoff_base_s: float = 0.4,
        batch_size: int = 25,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        if not urls:
            raise ValueError("at least one RPC url is required")
        self._endpoints = [_Endpoint(u, TokenBucket(requests_per_second)) for u in urls]
        self._http = httpx.AsyncClient(timeout=timeout_s, transport=transport)
        self._ids = itertools.count(1)
        self.max_attempts = max_attempts
        self.backoff_base_s = backoff_base_s
        self.batch_size = batch_size

    async def close(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "RpcClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # ------------------------------------------------------------------ core

    def _endpoint_order(self) -> list[_Endpoint]:
        healthy = [e for e in self._endpoints if e.healthy]
        unhealthy = sorted((e for e in self._endpoints if not e.healthy), key=lambda e: e.cooldown_until)
        return healthy + unhealthy

    def _pick(self, tried: set[str]) -> _Endpoint:
        """Best endpoint not yet tried in this request (restart the rotation when all were)."""
        order = self._endpoint_order()
        if len(tried) >= len(order):
            tried.clear()
        ep = next(e for e in order if e.url not in tried)
        tried.add(ep.url)
        return ep

    async def _post(self, ep: _Endpoint, payload: Any, cost: int) -> Any:
        await ep.limiter.acquire(cost)
        try:
            resp = await self._http.post(ep.url, json=payload, headers={"content-type": "application/json"})
        except httpx.HTTPError as exc:
            raise RpcTransportError(f"{ep.label}: {type(exc).__name__}: {exc}") from exc
        if resp.status_code in _TRANSIENT_HTTP:
            raise RpcTransportError(f"{ep.label}: HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError as exc:
            raise RpcTransportError(f"{ep.label}: non-JSON response (HTTP {resp.status_code})") from exc

    async def _backoff(self, attempt: int) -> None:
        delay = self.backoff_base_s * (2**attempt) * (0.75 + random.random() / 2)
        await asyncio.sleep(min(delay, 8.0))

    async def call(self, method: str, params: list[Any] | None = None) -> Any:
        params = params or []
        last_exc: Exception | None = None
        failures: list[str] = []
        tried: set[str] = set()
        for attempt in range(self.max_attempts):
            ep = self._pick(tried)
            payload = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params}
            try:
                body = await self._post(ep, payload, 1)
            except RpcTransportError as exc:
                ep.mark_failure()
                last_exc = exc
                failures.append(str(exc))
                log.debug("rpc transport error on %s (%s): %s", ep.label, method, exc)
                await self._backoff(attempt)
                continue
            if isinstance(body, dict) and "error" in body and body["error"] is not None:
                err = body["error"]
                rpc_err = RpcError(err.get("code"), err.get("message", ""), err.get("data"), ep.label)
                if rpc_err.deterministic:
                    ep.mark_success()
                    raise rpc_err
                ep.mark_failure()
                last_exc = rpc_err
                failures.append(f"{ep.label}: {rpc_err}")
                log.debug("rpc error on %s (%s): %s", ep.label, method, rpc_err)
                await self._backoff(attempt)
                continue
            ep.mark_success()
            return body.get("result") if isinstance(body, dict) else body
        assert last_exc is not None
        # Surface every endpoint's answer: the useful one ("archive requests
        # require a token") is often not the last.
        unique = list(dict.fromkeys(failures))
        if isinstance(last_exc, RpcError):
            raise RpcError(last_exc.code, f"{method} failed on all endpoints: " + " | ".join(unique), last_exc.data)
        raise RpcTransportError(f"{method} failed on all endpoints: " + " | ".join(unique))

    async def batch(self, calls: Sequence[tuple[str, list[Any]]]) -> list[Any]:
        """Execute calls as JSON-RPC batches. Returns results in order.

        Per-item deterministic errors are returned as ``RpcError`` instances
        (not raised). Items that fail transiently are retried individually.
        """
        results: list[Any] = [None] * len(calls)
        for start in range(0, len(calls), self.batch_size):
            chunk = list(enumerate(calls[start : start + self.batch_size], start=start))
            chunk_results = await self._batch_chunk(chunk)
            for idx, value in chunk_results.items():
                results[idx] = value
        return results

    async def _batch_chunk(self, chunk: list[tuple[int, tuple[str, list[Any]]]]) -> dict[int, Any]:
        last_exc: Exception | None = None
        tried: set[str] = set()
        for attempt in range(self.max_attempts):
            ep = self._pick(tried)
            id_map: dict[int, int] = {}
            payload = []
            for idx, (method, params) in chunk:
                rid = next(self._ids)
                id_map[rid] = idx
                payload.append({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            try:
                body = await self._post(ep, payload, len(payload))
            except RpcTransportError as exc:
                ep.mark_failure()
                last_exc = exc
                await self._backoff(attempt)
                continue
            if not isinstance(body, list):
                # Endpoint rejected batching (or returned a single error object).
                ep.mark_failure()
                last_exc = RpcTransportError(f"{ep.label}: batch not supported: {str(body)[:200]}")
                await self._backoff(attempt)
                continue
            ep.mark_success()
            out: dict[int, Any] = {}
            retry_individually: list[int] = []
            for item in body:
                idx = id_map.get(item.get("id"))
                if idx is None:
                    continue
                if item.get("error") is not None:
                    err = item["error"]
                    rpc_err = RpcError(err.get("code"), err.get("message", ""), err.get("data"), ep.label)
                    if rpc_err.deterministic:
                        out[idx] = rpc_err
                    else:
                        retry_individually.append(idx)
                else:
                    out[idx] = item.get("result")
            missing = [idx for idx in id_map.values() if idx not in out and idx not in retry_individually]
            retry_individually.extend(missing)
            if retry_individually:
                lookup = dict(chunk)
                for idx in retry_individually:
                    method, params = lookup[idx]
                    try:
                        out[idx] = await self.call(method, params)
                    except RpcError as exc:
                        out[idx] = exc
            return out
        assert last_exc is not None
        raise last_exc

    # --------------------------------------------------------------- helpers

    async def chain_id(self) -> int:
        return int(await self.call("eth_chainId"), 16)

    async def block_number(self) -> int:
        return int(await self.call("eth_blockNumber"), 16)

    async def get_block(self, number: int | str, full: bool = False) -> dict[str, Any]:
        tag = hex(number) if isinstance(number, int) else number
        block = await self.call("eth_getBlockByNumber", [tag, full])
        if block is None:
            raise RpcError(None, f"block {number} not found")
        return block

    async def gas_price(self) -> int:
        return int(await self.call("eth_gasPrice"), 16)

    async def eth_call(
        self,
        to: str,
        data: str,
        block: int | str = "latest",
        *,
        from_: str | None = None,
        value: int = 0,
        gas: int | None = None,
        state_override: dict[str, Any] | None = None,
    ) -> str:
        tx: dict[str, Any] = {"to": to, "data": data}
        if from_:
            tx["from"] = from_
        if value:
            tx["value"] = hex(value)
        if gas:
            tx["gas"] = hex(gas)
        tag = hex(block) if isinstance(block, int) else block
        params: list[Any] = [tx, tag]
        if state_override:
            params.append(state_override)
        return await self.call("eth_call", params)

    async def get_logs(self, flt: dict[str, Any]) -> list[dict[str, Any]]:
        return await self.call("eth_getLogs", [flt])


def chunked(seq: Sequence[Any] | Iterable[Any], size: int) -> list[list[Any]]:
    items = list(seq)
    return [items[i : i + size] for i in range(0, len(items), size)]
