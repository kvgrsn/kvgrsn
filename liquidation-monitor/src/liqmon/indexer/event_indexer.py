"""Incremental, reorg-aware event indexer used for position discovery.

Adapters describe *which* logs reveal a position (``EventSpec``); this module
handles *how* to fetch them efficiently:

* resumes from the last checkpoint instead of rescanning history,
* stays ``confirmations`` blocks behind head,
* adapts the eth_getLogs block span to the endpoint's limits,
* detects reorgs by re-checking stored block hashes and rolls back.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from ..config import ChainConfig
from ..db.store import PositionEvent, Store
from ..rpc.client import RpcClient, RpcError

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class EventSpec:
    kind: str
    address: str
    topic0: str
    # Returns the position owner encoded in the log (or None to skip it).
    account_of: Callable[[dict[str, Any]], str | None]


@dataclass
class IndexStats:
    from_block: int
    to_block: int
    events: int
    new_positions: int
    reorg_rollback_to: int | None = None


async def fetch_logs_adaptive(
    client: RpcClient,
    addresses: Sequence[str],
    topic0s: Sequence[str],
    start: int,
    end: int,
    max_range: int,
    min_range: int = 1,
) -> list[dict[str, Any]]:
    """Fetch logs for [start, end], halving the span on range errors."""
    out: list[dict[str, Any]] = []
    step = max_range
    cur = start
    while cur <= end:
        hi = min(end, cur + step - 1)
        flt = {
            "address": list(addresses) if len(addresses) > 1 else addresses[0],
            "fromBlock": hex(cur),
            "toBlock": hex(hi),
            "topics": [list(topic0s)],
        }
        try:
            logs = await client.get_logs(flt)
        except RpcError as exc:
            if exc.is_range_error and step > min_range:
                step = max(min_range, step // 2)
                log.debug("getLogs range error, shrinking span to %d: %s", step, exc)
                continue
            raise
        out.extend(logs)
        cur = hi + 1
        if step < max_range:
            step = min(max_range, step * 2)
    return out


class EventIndexer:
    def __init__(
        self,
        client: RpcClient,
        store: Store,
        chain: ChainConfig,
        protocol_id: str,
        specs: Sequence[EventSpec],
        deployment_block: int | None,
        window_concurrency: int = 4,
    ):
        if not specs:
            raise ValueError("no event specs")
        self.client = client
        self.store = store
        self.chain = chain
        self.protocol_id = protocol_id
        self.specs = list(specs)
        self.deployment_block = deployment_block
        self.stream = f"{protocol_id}:positions"
        self.window_concurrency = window_concurrency
        self._by_topic = {s.topic0.lower(): s for s in self.specs}

    async def check_reorg(self) -> int | None:
        """Return the block rolled back to, or None if the stored chain is intact."""
        stored = self.store.recent_block_hashes(self.stream)
        if not stored:
            return None
        latest_number, latest_hash = stored[0]
        block = await self.client.get_block(latest_number)
        if block["hash"].lower() == latest_hash.lower():
            return None
        fork_point = None
        for number, h in stored[1:]:
            b = await self.client.get_block(number)
            if b["hash"].lower() == h.lower():
                fork_point = number
                break
        if fork_point is None:
            oldest = stored[-1][0]
            fork_point = max(0, oldest - self.chain.reorg_depth)
        removed = self.store.rollback(self.stream, self.protocol_id, fork_point)
        log.warning(
            "%s: reorg detected at block %d, rolled back to %d (%d events discarded)",
            self.protocol_id,
            latest_number,
            fork_point,
            removed,
        )
        return fork_point

    def _parse(self, logs: list[dict[str, Any]]) -> list[PositionEvent]:
        events = []
        for lg in logs:
            if lg.get("removed"):
                continue
            spec = self._by_topic.get(lg["topics"][0].lower())
            if spec is None:
                continue
            account = spec.account_of(lg)
            if not account:
                continue
            events.append(
                PositionEvent(
                    protocol_id=self.protocol_id,
                    account=account,
                    kind=spec.kind,
                    block_number=int(lg["blockNumber"], 16),
                    block_hash=lg["blockHash"],
                    tx_hash=lg["transactionHash"],
                    log_index=int(lg["logIndex"], 16),
                )
            )
        return events

    async def sync(
        self,
        *,
        from_block: int | None = None,
        to_block: int | None = None,
        lookback: int | None = None,
        progress: Callable[[int, int], None] | None = None,
    ) -> IndexStats:
        rolled_back = await self.check_reorg()
        head = await self.client.block_number()
        safe_head = head - self.chain.confirmations
        end = min(to_block, safe_head) if to_block is not None else safe_head

        checkpoint = self.store.get_checkpoint(self.stream)
        if from_block is not None:
            start = from_block
        elif checkpoint is not None:
            start = checkpoint + 1
        elif lookback is not None:
            start = max(0, end - lookback)
        elif self.deployment_block is not None:
            start = self.deployment_block
        else:
            raise ValueError(
                f"{self.protocol_id}: no checkpoint and no deployment_block configured; "
                "pass --from-block or --lookback"
            )

        before = len(self.store.positions(self.protocol_id, include_closed=True))
        total_events = 0
        if start <= end:
            addresses = sorted({s.address for s in self.specs})
            topics = sorted({s.topic0 for s in self.specs})
            # Process fixed-size windows a few at a time; commit the checkpoint
            # only after every window of a batch is stored.
            window = self.chain.max_log_range * 8
            windows = [(s, min(end, s + window - 1)) for s in range(start, end + 1, window)]
            for i in range(0, len(windows), self.window_concurrency):
                batch = windows[i : i + self.window_concurrency]
                results = await asyncio.gather(
                    *(
                        fetch_logs_adaptive(self.client, addresses, topics, s, e, self.chain.max_log_range)
                        for s, e in batch
                    )
                )
                events = [ev for logs in results for ev in self._parse(logs)]
                total_events += self.store.add_events(events)
                batch_end = batch[-1][1]
                is_last = batch_end == end
                block_hash = (await self.client.get_block(batch_end))["hash"] if is_last else None
                self.store.set_checkpoint(self.stream, batch_end, block_hash, keep_hashes=self.chain.reorg_depth)
                if progress:
                    progress(batch_end - start + 1, end - start + 1)
        after = len(self.store.positions(self.protocol_id, include_closed=True))
        return IndexStats(start, end, total_events, after - before, rolled_back)
