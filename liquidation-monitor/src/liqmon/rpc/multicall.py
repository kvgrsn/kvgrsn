"""Multicall3 aggregate3 wrapper.

All reads of one scan cycle are pinned to a single block number so that
positions, prices and reserve state are mutually consistent.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode

from .abi import Fn, hex_to_bytes, selector
from .client import RpcClient, RpcError

_AGGREGATE3 = selector("aggregate3((address,bool,bytes)[])")


@dataclass(frozen=True)
class Call:
    target: str
    fn: Fn
    args: tuple[Any, ...] = ()
    allow_failure: bool = True

    def calldata(self) -> bytes:
        return self.fn.encode_bytes(*self.args)


@dataclass(frozen=True)
class CallResult:
    success: bool
    value: Any  # decoded output tuple (or single value) on success, raw bytes on failure


class Multicall:
    def __init__(self, client: RpcClient, address: str, chunk_size: int = 150, concurrency: int = 4):
        self.client = client
        self.address = address
        self.chunk_size = chunk_size
        self._sem = asyncio.Semaphore(concurrency)

    async def _aggregate(self, calls: Sequence[Call], block: int | str) -> list[tuple[bool, bytes]]:
        payload = [(c.target, c.allow_failure, c.calldata()) for c in calls]
        data = "0x" + (_AGGREGATE3 + abi_encode(["(address,bool,bytes)[]"], [payload])).hex()
        async with self._sem:
            raw = await self.client.eth_call(self.address, data, block)
        (results,) = abi_decode(["(bool,bytes)[]"], hex_to_bytes(raw))
        return [(bool(ok), bytes(ret)) for ok, ret in results]

    async def run(
        self,
        calls: Sequence[Call],
        block: int | str = "latest",
        *,
        unwrap_single: bool = True,
    ) -> list[CallResult]:
        chunks = [calls[i : i + self.chunk_size] for i in range(0, len(calls), self.chunk_size)]
        chunk_results = await asyncio.gather(*(self._run_chunk(ch, block) for ch in chunks))
        flat: list[tuple[bool, bytes]] = [r for ch in chunk_results for r in ch]
        out: list[CallResult] = []
        for call, (ok, ret) in zip(calls, flat):
            if not ok or (call.fn.outputs and len(ret) == 0):
                out.append(CallResult(False, ret))
                continue
            try:
                decoded = call.fn.decode(ret) if call.fn.outputs else ()
            except Exception:  # noqa: BLE001 - malformed return data = failure
                out.append(CallResult(False, ret))
                continue
            if unwrap_single and len(decoded) == 1:
                out.append(CallResult(True, decoded[0]))
            else:
                out.append(CallResult(True, decoded))
        return out

    async def _run_chunk(self, calls: Sequence[Call], block: int | str) -> list[tuple[bool, bytes]]:
        try:
            return await self._aggregate(calls, block)
        except RpcError as exc:
            # A single allow_failure=False call reverting takes down the chunk;
            # also some nodes cap eth_call gas. Split and retry.
            if len(calls) == 1:
                if exc.is_revert:
                    return [(False, b"")]
                raise
            mid = len(calls) // 2
            left = await self._run_chunk(calls[:mid], block)
            right = await self._run_chunk(calls[mid:], block)
            return left + right

    async def map(
        self,
        fn: Fn,
        targets_args: Sequence[tuple[str, tuple[Any, ...]]],
        block: int | str = "latest",
        transform: Callable[[Any], Any] | None = None,
    ) -> list[Any | None]:
        calls = [Call(t, fn, a) for t, a in targets_args]
        res = await self.run(calls, block)
        return [(transform(r.value) if transform else r.value) if r.success else None for r in res]
