"""Transaction executor: the only module that can sign or broadcast.

It is deliberately separate from the scanner and refuses to broadcast
unless every interlock passes:

1. ``dry_run`` is false (``LIQMON_DRY_RUN=false``),
2. the caller passed ``broadcast=True`` (CLI ``--broadcast``),
3. ``LIQMON_PRIVATE_KEY`` is set in the environment (never in files),
4. ``LIQMON_EXECUTOR_<CHAIN>`` is a deployed AaveV3LiquidationExecutor whose
   on-chain ``owner()`` equals the key's address,
5. a fresh re-quote at the latest block reproduces the opportunity, and a
   fork simulation of the *exact* transaction (deployed executor, real
   owner, final minProfit) succeeds.

Without all five it prints what it would do and stops. Use a private /
MEV-protected submission endpoint via ``LIQMON_TX_RPC_<CHAIN>``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from eth_utils import to_checksum_address

from ..config import load_chains, load_dex, load_protocols, load_settings
from ..db.store import Store
from ..engine.profit import usd_to_units
from ..engine.scanner import ChainScanner
from ..models import SimStatus
from ..rpc.abi import Fn
from ..rpc.client import RpcClient
from ..simulation.aave_v3 import executor_calldata, simulate_aave_v3_liquidation

log = logging.getLogger(__name__)

OWNER = Fn("owner()", ["address"])
POOL = Fn("pool()", ["address"])


@dataclass
class ExecutionOutcome:
    broadcast: bool
    reasons: list[str] = field(default_factory=list)
    tx: dict[str, Any] | None = None
    tx_hash: str | None = None
    receipt_status: int | None = None
    simulated_profit_usd: float | None = None


async def execute_opportunity(opportunity_id: int, *, broadcast: bool) -> ExecutionOutcome:
    settings = load_settings()
    store = Store(settings.db_path)
    out = ExecutionOutcome(broadcast=False)
    try:
        opp = store.get_opportunity(opportunity_id)
        if opp is None:
            out.reasons.append(f"opportunity {opportunity_id} not found")
            return out
        chain = load_chains()[opp["chain"]]
        specs = [s for s in load_protocols() if s.id == opp["protocol_id"]]
        if not specs:
            out.reasons.append(f"protocol {opp['protocol_id']} not in registry")
            return out
        if specs[0].adapter != "aave_v3":
            out.reasons.append("executor currently supports only the aave_v3 adapter")
            return out

        # ---- interlocks 1-4 (evaluated up front, reported together)
        if settings.dry_run:
            out.reasons.append("dry_run is true (set LIQMON_DRY_RUN=false to allow broadcasting)")
        if not broadcast:
            out.reasons.append("--broadcast not given")
        key = os.environ.get("LIQMON_PRIVATE_KEY")
        executor_addr = os.environ.get(f"LIQMON_EXECUTOR_{chain.key.upper()}")
        sender = None
        if not key:
            out.reasons.append("LIQMON_PRIVATE_KEY not set")
        else:
            from eth_account import Account

            sender = Account.from_key(key).address
        if not executor_addr:
            out.reasons.append(f"LIQMON_EXECUTOR_{chain.key.upper()} not set (deploy contracts/src/AaveV3LiquidationExecutor.sol first)")

        scanner = ChainScanner(chain, specs, settings, store, load_dex().get(chain.key))
        try:
            await scanner.load()
            adapter = scanner.adapters[0]
            if executor_addr:
                executor_addr = to_checksum_address(executor_addr)
                owner = OWNER.decode_one(await scanner.client.eth_call(executor_addr, OWNER.encode()))
                pool = POOL.decode_one(await scanner.client.eth_call(executor_addr, POOL.encode()))
                if sender and owner.lower() != sender.lower():
                    out.reasons.append(f"executor owner {owner} != key address {sender}")
                if pool.lower() != adapter.pool.lower():  # type: ignore[attr-defined]
                    out.reasons.append(f"executor pool {pool} != protocol pool")

            # ---- interlock 5: fresh quote + exact simulation
            report = await scanner.run_cycle(simulate=False, sync_index=False, accounts=[opp["account"]])
            fresh = [
                o
                for o in report.opportunities
                if o.quote.collateral_asset == opp["collateral_asset"] and o.quote.debt_asset == opp["debt_asset"]
            ]
            if not fresh or fresh[0].funding is None:
                out.reasons.append("opportunity no longer reproduces at the latest block")
                return out
            o = fresh[0]
            debt_price = adapter.usd_price(o.quote.debt_asset) or 0.0
            native = adapter.native_usd_price() or 0.0
            gas_price = int(await scanner.client.gas_price() * settings.gas_price_multiplier)
            est_gas = o.estimate.gas_units if o.estimate else 1_000_000
            gas_usd = est_gas * gas_price / 1e18 * native
            # On-chain floor: profit in debt units must cover gas + threshold.
            min_profit = usd_to_units(settings.min_profit_usd + gas_usd, debt_price, o.quote.debt_decimals)
            plan = adapter.build_liquidation_transaction(o.quote, o.swap, o.funding, min_profit)
            await scanner.simulator.prepare(report.block, gas_price, native)
            sim = await simulate_aave_v3_liquidation(
                adapter,  # type: ignore[arg-type]
                plan,
                scanner.simulator,
                deployed_executor=executor_addr,
                sender=sender,
            )
            out.simulated_profit_usd = sim.profit_usd
            if sim.status != SimStatus.SUCCESS:
                out.reasons.append(f"final simulation {sim.status.value}: {sim.revert_reason or sim.detail}")
                return out

            if not executor_addr or not sender:
                return out
            client = scanner.client
            nonce = int(await client.call("eth_getTransactionCount", [sender, "pending"]), 16)
            out.tx = {
                "chainId": chain.chain_id,
                "nonce": nonce,
                "to": executor_addr,
                "value": 0,
                "data": executor_calldata(plan.params),
                "gas": int((sim.gas_used or est_gas) * 1.3),
                "gasPrice": gas_price,
            }
            if out.reasons:
                return out  # dry run: show the tx that would be sent

            from eth_account import Account

            signed = Account.sign_transaction(out.tx, key)
            tx_rpc = os.environ.get(f"LIQMON_TX_RPC_{chain.key.upper()}")
            submit = RpcClient([tx_rpc]) if tx_rpc else client
            try:
                out.tx_hash = await submit.call("eth_sendRawTransaction", ["0x" + signed.raw_transaction.hex().removeprefix("0x")])
                out.broadcast = True
                for _ in range(60):
                    receipt = await client.call("eth_getTransactionReceipt", [out.tx_hash])
                    if receipt:
                        out.receipt_status = int(receipt["status"], 16)
                        break
                    await asyncio.sleep(2)
            finally:
                if submit is not client:
                    await submit.close()
            return out
        finally:
            await scanner.close()
    finally:
        store.close()
