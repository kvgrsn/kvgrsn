# Architecture

```
                 config/*.yaml  (chains, protocol registry, DEX venues, settings)
                        │
        ┌───────────────┴───────────────────────────────────────────────┐
        │                     ChainScanner (one per chain)              │
        │                                                               │
 RPC ◄──┤ rpc/        async JSON-RPC: batching, token-bucket rate limit, │
 pool   │             retry+backoff, endpoint failover, Multicall3     │
        │                                                               │
        │ indexer/    incremental eth_getLogs, adaptive span,           │──► SQLite (db/store.py)
        │             confirmations, block-hash reorg detection         │    checkpoints, block hashes,
        │                                                               │    position events, positions,
        │ protocols/  ProtocolAdapter interface + registry              │    opportunities
        │   aave_v3/  reference adapter (discovery events, state,       │
        │             eligibility, exact v3.7 math, funding, tx build)  │
        │                                                               │
        │ dex/        executable quotes (QuoterV2 / getAmountsOut),     │
        │             direct + 2-hop via connectors, price impact       │
        │ flash/      wallet vs flash choice, flash sources             │
        │ engine/     profit engine, ranking, scan pipeline             │
        │ simulation/ anvil fork manager, full-path Aave simulation,    │──► anvil (local fork)
        │             historical replay/backtest                        │
        │ auctions/   Dutch-auction math + read-only Clipper reader     │
        │ output/     rich dashboard, alerts (Telegram/Discord/desktop) │
        └───────────────────────────────────────────────────────────────┘

        executor/broadcaster.py: separate module and CLI command; the only code that signs.
        contracts/src/AaveV3LiquidationExecutor.sol: owner-gated atomic flash→liquidate→swap→repay.
```

## Pipeline per cycle

1. **Pin a block.** Every read in the cycle (positions, prices, reserve flags,
   DEX quotes) uses that block number, so the numbers are mutually consistent.
   The fork simulation forks the same block.
2. **Discovery.** `EventIndexer.sync()` fetches the adapter's `EventSpec`s from the
   last checkpoint up to `head - confirmations`. First it re-reads the stored block
   hashes; on mismatch it rolls back to the last matching block (`reorg_depth` bound).
3. **State.** `adapter.get_position_states(accounts, block)` makes one multicall per
   ~150 accounts for the protocol's own health computation. Per-asset balances are
   fetched only for accounts below `watch_health_factor`.
4. **Eligibility.** `adapter.is_liquidatable()` mirrors the protocol's validation:
   HF threshold, reserve active/paused, grace period, collateral enabled, self-liquidation.
   Accounts with HF < 1 that fail are reported as *blocked*, with the reason.
5. **Quote.** `adapter.get_liquidation_quotes()` runs the protocol's integer math for
   every (collateral, debt) pair, plans `debtToCover` (close factor, dust rule), checks
   the collateral reserve's liquidity, and keeps the best N pairs.
6. **Route.** `RouteFinder.best_route()` quotes the exact seized-collateral amount on
   every existing pool or path, pinned to the block, and picks the best output net of
   the swap's gas. Price impact is measured against a 1/1000 probe on the same path.
7. **Funding.** `flash.choose_funding()` uses wallet capital when the executor already
   holds enough of the debt asset (no fee), otherwise the cheapest flash source with
   enough liquidity.
8. **Profit estimate.** `engine.profit.estimate_profit()` computes swap out − slippage
   haircut − debt repaid − flash fee − gas − safety buffer. DEX fees and the protocol
   fee are shown but not double-counted.
9. **Simulation (mandatory).** The top candidates are simulated on an anvil fork of the
   pinned block. A fresh `AaveV3LiquidationExecutor` is deployed and the complete
   transaction runs (flash loan → liquidationCall → collateral received → swap →
   flash repayment). The result records success or revert (decoded custom errors),
   gas used, executor token deltas, the Pool's own `LiquidationCall` amounts, and
   profit net of gas. Only `SUCCESS` can be executable.
10. **Rank, store, display, alert.** Ranking puts simulated successes first, then sorts
    by profit, capital, gas, hops and price impact. Alerts fire only on
    `SUCCESS` with profit above the alert threshold.

## Execution separation

- The scanner has no key material and never signs.
- `liqmon execute --id N` re-quotes at the latest block, rebuilds the transaction with an
  on-chain `minProfit` floor (threshold + gas, in debt-asset units), and simulates it
  against the **deployed** executor as its real owner. It broadcasts only when `dry_run`
  is false, `--broadcast` is given, a key is in the environment, the executor's `owner()`
  matches the key, and that last simulation succeeds.
- The executor contract is owner-gated. Its flash-loan callback accepts only loans it
  initiated itself (`msg.sender == pool && initiator == this`). Any shortfall reverts
  the whole transaction.

## Reliability features

| Concern | Where | How |
|---|---|---|
| async I/O | `rpc/client.py` | httpx AsyncClient; concurrent multicall chunks and log windows |
| batching | `RpcClient.batch`, `Multicall` | JSON-RPC batches; Multicall3 `aggregate3` with allowFailure |
| rate limiting | `TokenBucket` | per endpoint; a batch costs one token per item |
| retries | `RpcClient.call` | exponential backoff with jitter; transient HTTP codes and RPC errors |
| failover | `_pick()` | healthiest untried endpoint first; failing endpoints cool down |
| deterministic errors | `RpcError.deterministic` | reverts and log-range errors are raised immediately |
| log limits | `fetch_logs_adaptive` | halves the span on range errors, grows it back after |
| reorgs | `EventIndexer.check_reorg` | compares stored block hashes and rolls back events and positions |
| consistency | scanner | every read of one cycle is pinned to one block |
| multicall failure isolation | `Multicall._run_chunk` | splits failing chunks, isolates reverting calls |

WebSockets: the loop polls `eth_blockNumber`/`latest` once per cycle. `newHeads` would cut
latency by at most one polling interval, and it doesn't change the finding in
[protocols/aave_v3.md](protocols/aave_v3.md): the valuable Aave liquidations are same-block
oracle back-runs, not end-of-block state changes.

## Adding a protocol

1. Research it (see the procedure in `PROTOCOL_RESEARCH.md`).
2. Implement `ProtocolAdapter` in `protocols/<name>/`. Port the math with its exact
   rounding and unit-test it.
3. Register it in `protocols/registry.py` and add registry entries in `config/protocols.yaml`.
4. If execution needs a different call pattern, add an executor contract and a simulation
   module next to `simulation/aave_v3.py`.
5. Backtest with `liqmon replay` against real historical liquidations.

Auction protocols implement `get_auction_state()` / `list_active_auctions()` using
`auctions/dutch.py`, which already ports the abacus price functions and `Clipper.status`
and reads every parameter from the deployment.

## Storage schema

See `db/store.py`. SQLite in WAL mode is enough for one machine. The store interface
is small (checkpoints, events, positions, opportunities) so it can be moved to PostgreSQL
without touching the adapters.
