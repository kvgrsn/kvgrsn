"""Per-chain scan pipeline.

discover (incremental index) -> position state (pinned block, multicall)
-> eligibility (protocol's own condition) -> liquidation quote (protocol
math) -> executable swap route -> funding (wallet vs flash) -> profit
estimate -> fork simulation (mandatory for "executable") -> rank -> store.

The scanner never signs or broadcasts anything.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Callable

from ..config import ChainConfig, DexConfig, ProtocolSpec, Settings
from ..db.store import Store
from ..dex.router import RouteFinder
from ..indexer.event_indexer import EventIndexer, IndexStats
from ..models import Eligibility, Opportunity, PositionState, SimStatus, SimulationResult
from ..protocols.base import AdapterContext, ProtocolAdapter
from ..protocols.registry import build_adapter
from ..rpc.abi import ERC20_BALANCE_OF
from ..rpc.client import RpcClient
from ..rpc.multicall import Call, Multicall
from ..simulation.anvil import AnvilError
from ..simulation.simulator import ForkSimulator
from .profit import estimate_gas, estimate_profit
from .ranking import rank

log = logging.getLogger(__name__)


@dataclass
class CycleReport:
    chain: str
    block: int
    timestamp: int
    duration_s: float = 0.0
    positions_checked: int = 0
    liquidatable: int = 0
    opportunities: list[Opportunity] = field(default_factory=list)
    watch: list[PositionState] = field(default_factory=list)
    blocked: list[tuple[PositionState, Eligibility]] = field(default_factory=list)
    index_stats: dict[str, IndexStats] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


class ChainScanner:
    def __init__(
        self,
        chain: ChainConfig,
        specs: list[ProtocolSpec],
        settings: Settings,
        store: Store,
        dex: DexConfig | None,
        *,
        anvil_port: int | None = None,
        rpc_timeout_s: float = 30.0,
    ):
        self.chain = chain
        self.specs = [s for s in specs if s.chain == chain.key and s.enabled]
        self.settings = settings
        self.store = store
        self.client = RpcClient(
            chain.rpc_urls,
            requests_per_second=chain.requests_per_second,
            batch_size=chain.rpc_batch_size,
            timeout_s=rpc_timeout_s,
        )
        self.multicall = Multicall(self.client, chain.multicall3, chunk_size=chain.multicall_chunk)
        self.ctx = AdapterContext(chain=chain, client=self.client, multicall=self.multicall, settings=settings)
        self.adapters: list[ProtocolAdapter] = [build_adapter(self.ctx, s) for s in self.specs]
        self.routes = RouteFinder(self.client, chain.multicall3, dex) if dex else None
        self.simulator = ForkSimulator(chain, settings, port=anvil_port or settings.anvil_port)
        self.executor_address = os.environ.get(f"LIQMON_EXECUTOR_{chain.key.upper()}")
        self._loaded = False

    async def close(self) -> None:
        await self.simulator.close()
        await self.client.close()

    async def load(self) -> None:
        if self._loaded:
            return
        for a in self.adapters:
            await a.load()
        self._loaded = True

    def indexer_for(self, adapter: ProtocolAdapter) -> EventIndexer:
        return EventIndexer(
            self.client,
            self.store,
            self.chain,
            adapter.protocol_id,
            adapter.discovery_events(),
            adapter.spec.deployment_block,
        )

    async def index(
        self,
        *,
        from_block: int | None = None,
        lookback: int | None = None,
        progress: Callable[[int, int], None] | None = None,
    ) -> dict[str, IndexStats]:
        await self.load()
        stats = {}
        for a in self.adapters:
            stats[a.protocol_id] = await self.indexer_for(a).sync(
                from_block=from_block, lookback=lookback, progress=progress
            )
        return stats

    async def _wallet_balances(self, pairs: set[str], block: int) -> dict[str, int]:
        if not self.executor_address or not pairs:
            return {}
        assets = sorted(pairs)
        res = await self.multicall.run([Call(a, ERC20_BALANCE_OF, (self.executor_address,)) for a in assets], block)
        return {a: int(r.value) for a, r in zip(assets, res) if r.success}

    async def run_cycle(
        self,
        *,
        simulate: bool = True,
        sync_index: bool = True,
        accounts: list[str] | None = None,
        block: int | None = None,
        gas_price_wei: int | None = None,
    ) -> CycleReport:
        """One scan. ``block`` pins a historical block (replay/backtest; needs
        an archive-capable RPC) and disables index syncing."""
        t0 = time.monotonic()
        await self.load()
        pinned = block is not None
        block_info = await self.client.get_block(block if pinned else "latest")
        block = int(block_info["number"], 16)
        ts = int(block_info["timestamp"], 16)
        report = CycleReport(chain=self.chain.key, block=block, timestamp=ts)
        if pinned:
            sync_index = False
        base_fee = block_info.get("baseFeePerGas")
        if gas_price_wei is not None:
            gas_price = gas_price_wei
        elif pinned and base_fee:
            gas_price = int(base_fee, 16)
        else:
            gas_price = await self.client.gas_price()

        opportunities: list[Opportunity] = []
        for adapter in self.adapters:
            pid = adapter.protocol_id
            if sync_index and accounts is None:
                try:
                    report.index_stats[pid] = await self.indexer_for(adapter).sync()
                except ValueError as exc:  # no checkpoint / deployment block yet
                    report.errors.append(f"{pid}: {exc}")
                except Exception as exc:  # noqa: BLE001 - indexing lag must not stop scanning
                    report.errors.append(f"{pid}: indexing failed: {exc}")
            await adapter.refresh_market(block)

            accts = accounts if accounts is not None else [p.account for p in self.store.positions(pid)]
            states = await adapter.get_position_states(accts, block)
            report.positions_checked += len(states)
            if accounts is None:
                self.store.update_position_health(
                    pid,
                    [
                        (
                            s.account,
                            "active" if s.has_debt else "closed",
                            s.health_factor,
                            s.total_collateral_usd,
                            s.total_debt_usd,
                        )
                        for s in states
                    ],
                    block,
                )

            native_usd = adapter.native_usd_price() or 0.0
            candidates = []
            for s in states:
                if s.health_factor is None:
                    continue
                if s.health_factor < 1:
                    candidates.append(s)
                elif s.health_factor < self.settings.watch_health_factor:
                    report.watch.append(s)

            for state in candidates:
                elig = adapter.is_liquidatable(state, ts, liquidator=self.executor_address)
                if not elig.liquidatable:
                    report.blocked.append((state, elig))
                    continue
                report.liquidatable += 1
                quotes = await adapter.get_liquidation_quotes(state, self.settings.max_pairs_per_position)
                wallet = await self._wallet_balances({q.debt_asset for q in quotes}, block)
                for q in quotes:
                    opp = await self._evaluate_quote(adapter, state, q, wallet.get(q.debt_asset), gas_price, native_usd, block)
                    opportunities.append(opp)

        # Simulate the most promising candidates (mandatory for "executable").
        ranked = rank(opportunities)
        if simulate:
            to_sim = [
                o
                for o in ranked
                if o.rejected_reason is None
                and o.estimate is not None
                and o.estimate.expected_profit_usd > -abs(self.settings.min_profit_usd)
            ][: self.settings.max_simulations_per_cycle]
            if to_sim:
                await self._simulate(to_sim, block, gas_price, report)
        for o in opportunities:
            if o.simulation.status == SimStatus.SUCCESS and o.rejected_reason is None:
                net = (o.simulation.profit_usd or 0.0) - (o.estimate.safety_buffer_usd if o.estimate else 0.0)
                if net < self.settings.min_profit_usd:
                    o.rejected_reason = f"simulated profit ${net:,.2f} (after safety buffer) below threshold"
            elif o.rejected_reason is None and o.estimate and o.estimate.expected_profit_usd < self.settings.min_profit_usd:
                o.rejected_reason = f"estimated profit ${o.estimate.expected_profit_usd:,.2f} below threshold"
        report.opportunities = rank(opportunities)
        for o in report.opportunities:
            # Keep the database to things worth auditing: simulated or accepted.
            if o.simulation.status != SimStatus.SKIPPED or o.rejected_reason is None:
                o.opportunity_id = self._persist(o)
        report.duration_s = time.monotonic() - t0
        return report

    async def _evaluate_quote(self, adapter, state, q, wallet_balance, gas_price, native_usd, block) -> Opportunity:
        opp = Opportunity(
            quote=q,
            health_factor=state.health_factor,
            swap=None,
            funding=None,
            estimate=None,
            simulation=SimulationResult(status=SimStatus.SKIPPED),
        )
        funding = await adapter.plan_funding(q, wallet_balance)
        if funding is None:
            opp.rejected_reason = "no funding: wallet balance too low and no flash liquidity for debt asset"
            return opp
        opp.funding = funding
        debt_usd = adapter.usd_price(q.debt_asset) or 0.0
        col_usd = adapter.usd_price(q.collateral_asset) or 0.0
        if q.collateral_asset != q.debt_asset:
            if self.routes is None:
                opp.rejected_reason = "no DEX config for chain"
                return opp

            def gas_to_out(units: int) -> int:
                if debt_usd <= 0:
                    return 0
                return int(units * gas_price / 1e18 * native_usd / debt_usd * 10**q.debt_decimals)

            best, _all = await self.routes.best_route(
                q.collateral_asset, q.debt_asset, q.expected_collateral_out, block, gas_cost_in_out_units=gas_to_out
            )
            if best is None:
                opp.rejected_reason = "no executable DEX route for collateral -> debt asset"
                return opp
            opp.swap = best
        gas_units = estimate_gas(q, opp.swap, funding)
        opp.estimate = estimate_profit(
            q,
            opp.swap,
            funding,
            debt_price_usd=debt_usd,
            collateral_price_usd=col_usd,
            native_price_usd=native_usd,
            gas_price_wei=gas_price,
            gas_units=gas_units,
            settings=self.settings,
        )
        return opp

    async def _simulate(self, opps: list[Opportunity], block: int, gas_price: int, report: CycleReport) -> None:
        adapters = {a.protocol_id: a for a in self.adapters}
        native_usd = next((a.native_usd_price() for a in self.adapters if a.native_usd_price()), 0.0) or 0.0
        try:
            await self.simulator.prepare(block, int(gas_price * self.settings.gas_price_multiplier), native_usd)
        except (AnvilError, OSError) as exc:
            report.errors.append(f"simulation unavailable: {exc}")
            for o in opps:
                o.simulation = SimulationResult(status=SimStatus.ERROR, detail=str(exc))
            return
        for o in opps:
            adapter = adapters[o.quote.protocol_id]
            assert o.funding is not None
            # minProfit=0 for measurement: we want the real number, not a revert.
            plan = adapter.build_liquidation_transaction(o.quote, o.swap, o.funding, min_profit=0)
            o.simulation = await adapter.simulate_liquidation(plan, self.simulator)

    def _persist(self, o: Opportunity) -> int:
        payload = {
            "quote": o.quote.__dict__ | {"liquidation_type": o.quote.liquidation_type.value},
            "health_factor": o.health_factor,
            "swap": o.swap.__dict__ if o.swap else None,
            "funding": o.funding.__dict__ if o.funding else None,
            "estimate": o.estimate.to_dict() if o.estimate else None,
            "simulation": o.simulation.__dict__ | {"status": o.simulation.status.value},
            "rejected_reason": o.rejected_reason,
        }
        return self.store.add_opportunity(
            o.quote.protocol_id,
            o.quote.chain,
            o.quote.account,
            o.quote.collateral_asset,
            o.quote.debt_asset,
            o.quote.block_number,
            o.simulation.status.value,
            o.profit_usd,
            payload,
        )
