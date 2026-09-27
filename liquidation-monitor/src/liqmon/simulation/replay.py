"""Backtest an adapter against a real historical liquidation.

Most liquidations on liquid markets are back-runs: the oracle update and
the liquidation land in the same block, so the state at the end of the
previous block is not yet liquidatable. To validate the full pipeline on the
real opportunity, this module rebuilds the state *just before* a given
liquidation transaction on a local anvil fork:

1. fork the parent block (archive RPC required),
2. replay every earlier transaction of the block (same sender, calldata,
   value and gas limit; zero gas price) into one block with the original
   timestamp,
3. hand that fork to the normal scanner as if it were the chain.

The replayed state is a reconstruction: replays that depend on gas price or
on exact coinbase balances can diverge, and the report lists any replayed
transaction whose status differs from the original.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from eth_abi import decode as abi_decode

from ..protocols.aave_v3.abi import LIQUIDATION_CALL_TOPIC
from ..rpc.abi import hex_to_bytes, topic_to_address
from ..rpc.client import RpcClient
from .anvil import AnvilFork


@dataclass
class ReplayContext:
    block: int
    tx_index: int
    timestamp: int
    borrower: str
    collateral_asset: str
    debt_asset: str
    actual_debt_repaid: int
    actual_collateral_seized: int
    actual_liquidator: str
    replayed: int = 0
    diverged: list[str] = field(default_factory=list)


async def rebuild_pre_tx_state(archive_rpc: str, tx_hash: str, fork: AnvilFork, pool: str) -> ReplayContext:
    src = RpcClient([archive_rpc], requests_per_second=10)
    try:
        receipt = await src.call("eth_getTransactionReceipt", [tx_hash])
        if receipt is None:
            raise ValueError(f"transaction {tx_hash} not found")
        liq = next(
            (
                lg
                for lg in receipt["logs"]
                if lg["address"].lower() == pool.lower() and lg["topics"][0].lower() == LIQUIDATION_CALL_TOPIC.lower()
            ),
            None,
        )
        if liq is None:
            raise ValueError("transaction has no Aave LiquidationCall event from this pool")
        dtc, seized, liquidator, _ = abi_decode(["uint256", "uint256", "address", "bool"], hex_to_bytes(liq["data"]))
        block_number = int(receipt["blockNumber"], 16)
        tx_index = int(receipt["transactionIndex"], 16)
        block = await src.get_block(block_number, full=True)
        prior = block["transactions"][:tx_index]
        original_status = {}
        for t in prior:
            r = await src.call("eth_getTransactionReceipt", [t["hash"]])
            original_status[t["hash"]] = int(r["status"], 16)
    finally:
        await src.close()

    ctx = ReplayContext(
        block=block_number,
        tx_index=tx_index,
        timestamp=int(block["timestamp"], 16),
        borrower=topic_to_address(liq["topics"][3]),
        collateral_asset=topic_to_address(liq["topics"][1]),
        debt_asset=topic_to_address(liq["topics"][2]),
        actual_debt_repaid=int(dtc),
        actual_collateral_seized=int(seized),
        actual_liquidator=liquidator,
    )

    await fork.start(block_number - 1)
    c = fork.client
    await c.call("evm_setAutomine", [False])
    await c.call("anvil_setNextBlockBaseFeePerGas", ["0x0"])
    await c.call("evm_setNextBlockTimestamp", [hex(ctx.timestamp)])
    sent: list[tuple[str, str]] = []
    for t in prior:
        tx: dict[str, Any] = {
            "from": t["from"],
            "data": t["input"],
            "value": t["value"],
            "gas": t["gas"],
            "gasPrice": "0x0",
        }
        if t.get("to"):
            tx["to"] = t["to"]
        try:
            h = await c.call("eth_sendTransaction", [tx])
            sent.append((t["hash"], h))
        except Exception as exc:  # noqa: BLE001 - report and continue
            ctx.diverged.append(f"{t['hash']}: not replayable ({exc})")
    await c.call("evm_mine")
    await c.call("evm_setAutomine", [True])
    for orig, new in sent:
        r = await c.call("eth_getTransactionReceipt", [new])
        status = int(r["status"], 16) if r else -1
        if status != original_status.get(orig):
            ctx.diverged.append(f"{orig}: replay status {status} != original {original_status.get(orig)}")
    ctx.replayed = len(sent)
    return ctx
