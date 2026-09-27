"""Command-line entry point.

    liqmon verify   [--chain bsc|avalanche]         read + print deployed protocol parameters
    liqmon index    --chain C [--from-block N | --lookback N]
    liqmon scan     [--chain C] [--loop] [--no-simulate] [--no-index]
    liqmon check    --chain C --account 0x.. [--no-simulate]
    liqmon auctions --chain C --clipper 0x..        read-only Dutch auction scan
    liqmon replay   --chain C --tx 0x..             backtest against a real historical liquidation
    liqmon execute  --id N [--broadcast]            separate executor (DRY_RUN by default)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from rich.console import Console
from rich.live import Live
from rich.table import Table

from .config import load_chains, load_dex, load_protocols, load_settings
from .db.store import Store
from .engine.scanner import ChainScanner, CycleReport
from .output.alerts import Alerter
from .output.dashboard import print_report, render_report

console = Console()


def _scanners(args: argparse.Namespace, store: Store) -> list[ChainScanner]:
    settings = load_settings()
    chains = load_chains()
    specs = load_protocols()
    dex = load_dex()
    keys = [args.chain] if getattr(args, "chain", None) else sorted({s.chain for s in specs if s.enabled})
    out = []
    for i, k in enumerate(keys):
        if k not in chains:
            raise SystemExit(f"unknown chain {k!r}; known: {', '.join(chains)}")
        out.append(ChainScanner(chains[k], specs, settings, store, dex.get(k), anvil_port=settings.anvil_port + i))
    return out


async def cmd_verify(args: argparse.Namespace) -> int:
    store = Store(":memory:")
    for sc in _scanners(args, store):
        try:
            await sc.load()
            for a in sc.adapters:
                d = a.describe()
                console.rule(f"[bold]{d['protocol_id']}[/] ({sc.chain.name})")
                for k, v in d.items():
                    if k != "reserves":
                        console.print(f"  {k:38s} {v}")
                t = Table(title="Reserves (read from chain)", show_lines=False)
                for c in ("id", "symbol", "decimals", "ltv", "liq_threshold", "liq_bonus", "protocol_fee",
                          "active", "paused", "frozen", "flashloan", "price_usd"):
                    t.add_column(c)
                for r in d["reserves"]:
                    t.add_row(*(str(r[c]) if c != "price_usd" else f"{r[c]:,.4f}" for c in (
                        "id", "symbol", "decimals", "ltv", "liq_threshold", "liq_bonus", "protocol_fee",
                        "active", "paused", "frozen", "flashloan", "price_usd")))
                console.print(t)
        finally:
            await sc.close()
    return 0


async def cmd_index(args: argparse.Namespace) -> int:
    settings = load_settings()
    store = Store(settings.db_path)
    try:
        for sc in _scanners(args, store):
            try:
                def progress(done: int, total: int) -> None:
                    console.print(f"  indexed {done:,}/{total:,} blocks", end="\r")

                stats = await sc.index(from_block=args.from_block, lookback=args.lookback, progress=progress)
                console.print()
                for pid, st in stats.items():
                    console.print(
                        f"{pid}: blocks {st.from_block:,}..{st.to_block:,}, {st.events} events, "
                        f"{st.new_positions} new positions, known={store.position_counts(pid)}"
                        + (f", reorg rollback to {st.reorg_rollback_to}" if st.reorg_rollback_to else "")
                    )
            finally:
                await sc.close()
    finally:
        store.close()
    return 0


async def _one_round(scanners: list[ChainScanner], simulate: bool, sync_index: bool) -> list[CycleReport]:
    reports = await asyncio.gather(
        *(sc.run_cycle(simulate=simulate, sync_index=sync_index) for sc in scanners), return_exceptions=True
    )
    out = []
    for sc, r in zip(scanners, reports):
        if isinstance(r, BaseException):
            rep = CycleReport(chain=sc.chain.key, block=0, timestamp=0)
            rep.errors.append(f"cycle failed: {type(r).__name__}: {r}")
            out.append(rep)
        else:
            out.append(r)
    return out


async def cmd_scan(args: argparse.Namespace) -> int:
    settings = load_settings()
    store = Store(settings.db_path)
    scanners = _scanners(args, store)
    alerter = Alerter(settings)
    try:
        if not args.loop:
            reports = await _one_round(scanners, not args.no_simulate, not args.no_index)
            print_report(reports, settings.min_profit_usd, console)
            await alerter.maybe_alert([o for r in reports for o in r.opportunities])
            return 0
        with Live(console=console, refresh_per_second=2, screen=False) as live:
            while True:
                reports = await _one_round(scanners, not args.no_simulate, not args.no_index)
                live.update(render_report(reports, settings.min_profit_usd))
                await alerter.maybe_alert([o for r in reports for o in r.opportunities])
                await asyncio.sleep(settings.scan_interval_s)
    finally:
        for sc in scanners:
            await sc.close()
        store.close()


async def cmd_check(args: argparse.Namespace) -> int:
    from eth_utils import to_checksum_address

    settings = load_settings()
    store = Store(settings.db_path)
    (sc,) = _scanners(args, store)
    try:
        account = to_checksum_address(args.account)
        report = await sc.run_cycle(
            simulate=not args.no_simulate, sync_index=False, accounts=[account], block=args.block
        )
        await sc.load()
        for a in sc.adapters:
            st = await a.get_position_state(account, report.block)
            console.rule(f"{a.protocol_id} {account} @ block {report.block}")
            hf = "no debt" if st.health_factor is None else f"{st.health_factor:.6f}"
            console.print(f"health factor {hf}  collateral ${st.total_collateral_usd:,.2f}  debt ${st.total_debt_usd:,.2f}")
            for c in st.collaterals:
                console.print(f"  collateral {c.symbol:10s} {c.amount / 10**c.decimals:>20,.6f}  ${c.value_usd:,.2f}")
            for d in st.debts:
                console.print(f"  debt       {d.symbol:10s} {d.amount / 10**d.decimals:>20,.6f}  ${d.value_usd:,.2f}")
            el = a.is_liquidatable(st, report.timestamp)
            console.print(f"liquidatable: {el.liquidatable}  {'; '.join(el.reasons + el.blockers)}")
        print_report([report], settings.min_profit_usd, console)
        for o in report.opportunities:
            console.print_json(json.dumps({
                "id": o.opportunity_id,
                "pair": f"{o.quote.collateral_symbol}->{o.quote.debt_symbol}",
                "debtToCover": str(o.quote.debt_to_cover_param),
                "expected_debt_repaid": o.quote.expected_debt_repaid,
                "expected_collateral_out": o.quote.expected_collateral_out,
                "notes": o.quote.notes,
                "swap": o.swap.__dict__ if o.swap else None,
                "funding": o.funding.__dict__ if o.funding else None,
                "estimate": o.estimate.to_dict() if o.estimate else None,
                "simulation": o.simulation.__dict__ | {"status": o.simulation.status.value},
                "rejected": o.rejected_reason,
            }, default=str))
    finally:
        await sc.close()
        store.close()
    return 0


async def cmd_auctions(args: argparse.Namespace) -> int:
    from .auctions.dutch import ClipperReader, params_summary
    from .rpc.client import RpcClient
    from .rpc.multicall import Multicall

    chain = load_chains()[args.chain]
    async with RpcClient(chain.rpc_urls, requests_per_second=chain.requests_per_second) as client:
        mc = Multicall(client, chain.multicall3)
        reader = ClipperReader(client, mc)
        params = await reader.params(args.clipper)
        console.print_json(json.dumps(params_summary(params), default=str))
        auctions = await reader.active_auctions(
            args.clipper, protocol_id="clipper", collateral_asset="?", debt_asset="?", oracle_price_ray=None
        )
        if not auctions:
            console.print("no active auctions")
        for a in auctions:
            console.print_json(json.dumps(a.__dict__, default=str))
        console.print(
            "[yellow]Read-only. Whether bidding is permitted depends on the deployment's access control "
            "(see docs/PROTOCOL_RESEARCH.md).[/]"
        )
    return 0


async def cmd_replay(args: argparse.Namespace) -> int:
    import dataclasses

    from .rpc.client import RpcClient
    from .simulation.anvil import AnvilFork
    from .simulation.replay import rebuild_pre_tx_state

    settings = load_settings()
    chain = load_chains()[args.chain]
    spec = next(s for s in load_protocols() if s.chain == chain.key and s.adapter == "aave_v3")
    archive = args.archive_rpc or chain.rpc_urls[0]
    port = settings.anvil_port + 10
    fork = AnvilFork(settings.anvil_bin, archive, port, startup_timeout_s=settings.simulation_timeout_s)
    store = Store(":memory:")
    sc = None
    try:
        ctx = await rebuild_pre_tx_state(archive, args.tx, fork, spec.addresses["pool"])
        console.print(
            f"rebuilt state before tx index {ctx.tx_index} of block {ctx.block} "
            f"({ctx.replayed} prior txs replayed, {len(ctx.diverged)} diverged)"
        )
        for d in ctx.diverged[:10]:
            console.print(f"  [yellow]! {d}[/]")
        async with RpcClient([archive]) as src:
            base_fee = (await src.get_block(ctx.block)).get("baseFeePerGas")
        fork_url = f"http://127.0.0.1:{port}"
        local = dataclasses.replace(chain, rpc_urls=(fork_url,), requests_per_second=1000.0, confirmations=0)
        # The fork fetches untouched historical state lazily, so allow slow calls.
        sc = ChainScanner(
            local, [spec], settings, store, load_dex().get(chain.key), anvil_port=port + 1, rpc_timeout_s=900
        )
        report = await sc.run_cycle(
            simulate=not args.no_simulate,
            sync_index=False,
            accounts=[ctx.borrower],
            gas_price_wei=int(base_fee, 16) if base_fee else None,
        )
        print_report([report], settings.min_profit_usd, console)
        console.rule("comparison with the real liquidation")
        console.print(
            f"actual: liquidator {ctx.actual_liquidator} repaid {ctx.actual_debt_repaid} of {ctx.debt_asset} "
            f"and seized {ctx.actual_collateral_seized} of {ctx.collateral_asset}"
        )
        for o in report.opportunities:
            same_pair = (o.quote.collateral_asset, o.quote.debt_asset) == (ctx.collateral_asset, ctx.debt_asset)
            console.print(
                f"ours:   {o.quote.collateral_symbol}->{o.quote.debt_symbol}{' (same pair)' if same_pair else ''}: "
                f"repay {o.quote.expected_debt_repaid}, seize {o.quote.expected_collateral_out}; "
                f"sim {o.simulation.status.value} repaid={o.simulation.debt_repaid} seized={o.simulation.collateral_seized} "
                f"gas={o.simulation.gas_used} profit=${(o.profit_usd or 0):,.2f}"
                + (f" revert={o.simulation.revert_reason}" if o.simulation.revert_reason else "")
                + (f" rejected={o.rejected_reason}" if o.rejected_reason else "")
            )
    finally:
        if sc is not None:
            await sc.close()
        await fork.stop()
        store.close()
    return 0


async def cmd_execute(args: argparse.Namespace) -> int:
    from .executor.broadcaster import execute_opportunity

    outcome = await execute_opportunity(args.id, broadcast=args.broadcast)
    if outcome.broadcast:
        console.print(f"[bold]broadcast[/] tx {outcome.tx_hash} status={outcome.receipt_status}")
    else:
        console.print("[yellow]NOT broadcast[/]:")
        for r in outcome.reasons:
            console.print(f"  - {r}")
        if outcome.simulated_profit_usd is not None:
            console.print(f"final simulated profit: ${outcome.simulated_profit_usd:,.2f}")
        if outcome.tx:
            console.print_json(json.dumps(outcome.tx, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="liqmon", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("verify", help="read and print deployed protocol parameters")
    s.add_argument("--chain")

    s = sub.add_parser("index", help="incremental position discovery from events")
    s.add_argument("--chain", required=True)
    g = s.add_mutually_exclusive_group()
    g.add_argument("--from-block", type=int)
    g.add_argument("--lookback", type=int, help="start this many blocks behind head if no checkpoint")

    s = sub.add_parser("scan", help="scan known positions, quote, simulate, rank")
    s.add_argument("--chain")
    s.add_argument("--loop", action="store_true")
    s.add_argument("--no-simulate", action="store_true")
    s.add_argument("--no-index", action="store_true")

    s = sub.add_parser("check", help="deep-dive one account")
    s.add_argument("--chain", required=True)
    s.add_argument("--account", required=True)
    s.add_argument("--block", type=int, help="replay at a historical block (archive RPC required)")
    s.add_argument("--no-simulate", action="store_true")

    s = sub.add_parser("auctions", help="read-only scan of a MakerDAO-style Clipper")
    s.add_argument("--chain", required=True)
    s.add_argument("--clipper", required=True)

    s = sub.add_parser("replay", help="backtest: rebuild state just before a real liquidation tx and run the pipeline")
    s.add_argument("--chain", required=True)
    s.add_argument("--tx", required=True, help="hash of a historical Aave liquidation transaction")
    s.add_argument("--archive-rpc", help="archive-capable RPC (default: first configured endpoint)")
    s.add_argument("--no-simulate", action="store_true")

    s = sub.add_parser("execute", help="separate executor; DRY_RUN unless every interlock passes")
    s.add_argument("--id", type=int, required=True)
    s.add_argument("--broadcast", action="store_true")

    args = p.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    handler = {
        "verify": cmd_verify,
        "index": cmd_index,
        "scan": cmd_scan,
        "check": cmd_check,
        "auctions": cmd_auctions,
        "replay": cmd_replay,
        "execute": cmd_execute,
    }[args.cmd]
    try:
        return asyncio.run(handler(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
