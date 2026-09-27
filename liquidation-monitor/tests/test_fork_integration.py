"""Network/fork integration tests (skipped by default).

Run with:  pytest -m "network or fork" -p no:cacheprovider
Needs public RPC access; the fork test also needs anvil and a compiled
executor (cd contracts && forge build) and an archive-capable Avalanche RPC.
"""

import dataclasses
import os

import pytest

from liqmon.config import load_chains, load_dex, load_protocols, load_settings
from liqmon.db.store import Store
from liqmon.engine.scanner import ChainScanner
from liqmon.models import SimStatus

# Real Aave V3 Avalanche liquidation: BTC.b collateral, WAVAX debt, block 95,757,847 (tx index 7),
# back-running a WAVAX price update at tx index 6 in the same block.
LIQ_TX = "0xf24fc5e7a384384b823f77cc09b807e767f979dc7095addfdb3289eda548b4d1"


@pytest.mark.network
async def test_verify_reads_both_aave_deployments():
    settings = load_settings()
    chains = load_chains()
    for key in ("bsc", "avalanche"):
        specs = [s for s in load_protocols() if s.chain == key]
        sc = ChainScanner(chains[key], specs, settings, Store(":memory:"), load_dex().get(key))
        try:
            await sc.load()
            d = sc.adapters[0].describe()
            assert d["pool_revision"] >= 11
            assert d["close_factor_hf_threshold"] == 950_000_000_000_000_000
            assert d["flash_premium_bps"] >= 0
            assert d["reserves"]
        finally:
            await sc.close()


@pytest.mark.fork
async def test_replay_real_liquidation_simulates_successfully():
    from liqmon.simulation.anvil import AnvilFork
    from liqmon.simulation.replay import rebuild_pre_tx_state

    settings = load_settings()
    chain = load_chains()["avalanche"]
    spec = next(s for s in load_protocols() if s.id == "aave_v3_avalanche")
    archive = os.environ.get("LIQMON_FORK_RPC_AVALANCHE", "https://api.avax.network/ext/bc/C/rpc")
    port = settings.anvil_port + 20
    fork = AnvilFork(settings.anvil_bin, archive, port, startup_timeout_s=120)
    sc = None
    try:
        ctx = await rebuild_pre_tx_state(archive, LIQ_TX, fork, spec.addresses["pool"])
        assert not ctx.diverged
        local = dataclasses.replace(chain, rpc_urls=(f"http://127.0.0.1:{port}",), requests_per_second=1000.0)
        sc = ChainScanner(local, [spec], settings, Store(":memory:"), load_dex()["avalanche"],
                          anvil_port=port + 1, rpc_timeout_s=900)
        report = await sc.run_cycle(simulate=True, sync_index=False, accounts=[ctx.borrower])
        assert report.liquidatable == 1
        pair = [o for o in report.opportunities
                if (o.quote.collateral_asset, o.quote.debt_asset) == (ctx.collateral_asset, ctx.debt_asset)]
        assert pair, "the real liquidation's pair must be among our quotes"
        o = pair[0]
        assert o.simulation.status == SimStatus.SUCCESS, o.simulation.revert_reason or o.simulation.detail
        # Our simulated liquidation repays the same close-factor-capped amount (± interest drift).
        assert abs(o.simulation.debt_repaid - ctx.actual_debt_repaid) / ctx.actual_debt_repaid < 0.001
    finally:
        if sc is not None:
            await sc.close()
        await fork.stop()
