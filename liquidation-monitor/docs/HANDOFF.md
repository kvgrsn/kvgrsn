# Handoff: state at the end of the cloud session (2026-09-27)

## Done and pushed (branch `claude/defi-liquidation-monitor-9sb37u`)

- Full pipeline for the Aave V3 reference adapter (BSC + Avalanche): discovery → eligibility →
  exact v3.7 math → DEX routing → wallet/flash funding → anvil fork simulation → dashboard/alerts.
  Separate executor with interlocks; `dry_run: true` by default. Nothing was ever broadcast.
- Tests: 38 pytest unit tests, 1 live network test (`pytest -m network`), 8 Foundry executor tests
  (`cd contracts && forge test`).
- Verified live: `liqmon verify`, `liqmon index`, `liqmon scan` on both chains (Avalanche ~240
  positions from a 300k-block window, BSC only ~2 h of history on public RPC).

## Local setup

```bash
git fetch origin && git checkout claude/defi-liquidation-monitor-9sb37u
cd liquidation-monitor
python3.11 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
foundryup && (cd contracts && forge build && forge test)
pytest && pytest -m network
cp .env.example .env   # set your own RPCs; keep LIQMON_DRY_RUN=true
```

## Open items

1. **Replay backtest not finished.** `liqmon replay --chain avalanche --tx 0xf24fc5e7…b4d1` rebuilds
   the pre-liquidation state correctly (7 prior txs replayed, 0 diverged, borrower HF 1.0018 → 0.99977).
   The pipeline then stalls on the free `api.avax.network` archive (~1 s per storage slot, anvil fetches
   lazily). Run it locally against **your own archive node** (`--archive-rpc`), optionally with
   `--venues uniswap_v3 traderjoe_v1 --connectors USDC`. Expected check: our quote pair BTC.b→WAVAX
   and `debt_repaid` ≈ the real liquidation's 6,783.67 WAVAX, simulation `SUCCESS`
   (`tests/test_fork_integration.py` automates this: `pytest -m fork`).
2. **Trader Joe Liquidity Book venue missing.** On Avalanche, 0.97 BTC.b → WAVAX gets 7,111.8 via
   Uniswap V3 (BTC.b→USDC→WAVAX, ~4.9% impact) vs ~7,450 fair. The deep BTC.b/AVAX liquidity is on LB.
   Add an LBQuoter-based venue in `dex/router.py` before trusting Avalanche profitability.
3. **Davos (BSC) research, started, needs addresses.** Verified on-chain: DUSD
   `0x8ec1877698acf262fe8ad8a295ad94d6ea258988`; abacus `0x74FB5adf4eBA704c42f5974B83E53BBDA46F0C96`
   = LinearDecrease `tau = 36000 s`; AuctionProxy lib `0x1c539E755A1BdaBB168aA9ad60B31548991981F9`.
   The public source (`davos-money/davos-contracts`) has `startAuction`/`buyFromAuction` without a whitelist
   and `drip` public, but the **deployed** Interaction must be checked. Needed: the BSC Interaction proxy and
   the MVT-ankrBNB collateral token address. Then read Clipper `buf/tail/cusp/chip/tip/count/kicks`,
   Spot `mat`, Vat `dust`, test `Interaction.drip` as a real transaction on an anvil fork (before/after
   `Jug.rho`, `Vat.rate`), and list open positions (historical logs need an archive/paid BSC RPC).
4. **BSC deployment block** for Aave V3 is still `null` in `config/protocols.yaml` (needs archive or BscScan key).

## Findings to keep in mind

- Every larger Aave V3 Avalanche liquidation checked was an oracle-update back-run in the same block;
  block-end polling rarely wins those.
- Lista DAO CDP auctions are keeper-whitelisted on-chain (`auctionWhitelistMode = 1`), contrary to its docs.
