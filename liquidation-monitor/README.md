# liqmon: liquidation monitoring and fork-simulation research (BSC + Avalanche)

A modular, local system that finds positions liquidatable **through each protocol's intended,
public liquidation mechanism**, prices the exit with executable DEX quotes, decides between
wallet capital and flash liquidity, and **proves every candidate with a full-path fork simulation**
before calling it executable. Scanning and broadcasting are separate. The scanner never signs,
and the executor defaults to dry run.

It does not bypass access control, touch oracles, or do anything outside the protocols' rules.
Keeper-restricted mechanisms are identified and reported, never worked around (see Lista DAO in
[docs/PROTOCOL_RESEARCH.md](docs/PROTOCOL_RESEARCH.md)).

## Status

| Component | State |
|---|---|
| RPC layer (async, batching, Multicall3, rate limit, retry, failover) | done |
| Incremental event indexer with reorg rollback | done |
| Adapter interface + registry | done |
| **Aave V3 reference adapter (BSC + Avalanche)** | done: discovery → eligibility → exact v3.7 math → routing → flash → simulation → output |
| DEX routing: Uniswap V3, PancakeSwap V3 (QuoterV2), PancakeSwap V2, Trader Joe V1, Pangolin | done; Trader Joe Liquidity Book not yet |
| Flash liquidity: Aave V3 `flashLoanSimple` | done; Balancer/V3-pool flash catalogued, not wired |
| Executor contract (`contracts/src/AaveV3LiquidationExecutor.sol`) | done, compiles; exercised on forks only |
| Anvil fork simulation + historical replay/backtest | done |
| Dutch-auction math + read-only Clipper reader | done, not tied to an executable adapter (the only Clipper found, Lista, is keeper-whitelisted) |
| Dashboard, alerts | done |
| Separate executor with interlocks | done; **never broadcast during development** |

## Repository assessment (before this work)

`kvgrsn/kvgrsn` is a GitHub profile repository. It had a profile `README.md` and
`Resources/Blank_SQL_Notebook.ipynb` (an unrelated SQL course notebook). There was no
liquidation code, so no existing architecture to keep, no unsafe assumptions to fix,
and nothing to reuse. This project lives in `liquidation-monitor/` so the profile page stays intact.

## Setup

```bash
cd liquidation-monitor
python3.11 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
curl -L https://foundry.paradigm.xyz | bash && foundryup   # anvil/forge for simulation
(cd contracts && forge build && forge test)                  # executor artifact + contract safety tests
pytest                                                       # Python unit tests (no network)
pytest -m network                                            # live read-only checks against both chains
pytest -m fork                                               # replay a real liquidation on an anvil fork (slow, archive RPC)
```

Use your own nodes where you can:

```bash
export LIQMON_RPC_BSC=http://127.0.0.1:8545                 # comma-separated list = failover order
export LIQMON_RPC_AVALANCHE=http://127.0.0.1:9650/ext/bc/C/rpc
export LIQMON_FORK_RPC_AVALANCHE=...                         # optional: separate endpoint for anvil forks
```

Public-endpoint limits measured on 2026-09-27: BSC publicnode serves logs only about 2 h back
without a token, `bsc-dataseed` rejects `eth_getLogs`, and 1rpc caps it at 50 blocks. A BSC backfill
therefore needs your own node or a paid endpoint. `api.avax.network` served archive state and
2048-block log ranges.

## Usage

```bash
liqmon verify                                  # read and print every deployed parameter the adapters use
liqmon index --chain avalanche                 # backfill from deployment_block (Avalanche: 11,970,506)
liqmon index --chain bsc --lookback 7000       # BSC: public RPC only allows recent history
liqmon scan                                    # one cycle, both chains, with simulation
liqmon scan --loop                             # live dashboard
liqmon check --chain avalanche --account 0x... [--block N]
liqmon replay --chain avalanche --tx 0x<historical liquidation tx> [--venues uniswap_v3 --connectors]
liqmon auctions --chain bsc --clipper 0x2dcFb02CE33955b6Cc0aF34033189DE3ac4C0292   # read-only
liqmon execute --id <opportunity id>           # dry run: shows the interlocks and the tx it would send
```

`replay` reads untouched historical state through anvil one storage slot at a time. On the free
Avalanche archive endpoint (~0.7–1.7 s per read) a full multi-venue route search can take hours. Restrict
routing with `--venues`/`--connectors` (empty `--connectors` = direct pools only) or point
`--archive-rpc` at your own archive node. Anvil caches fetched state under `~/.foundry/cache/rpc/`.

Dashboard columns: CHAIN | PROTOCOL | POSITION | COLLATERAL | DEBT | HF/CR | LIQ TYPE | AUCTION ID |
DISCOUNT (vs oracle) | CAPITAL REQ | FLASH | EST GAS | EXP PROFIT | SIM STATUS.
It also shows a watchlist (HF below `watch_health_factor`) and positions with HF < 1 that are blocked (with the protocol rule that blocks them).

## Configuration

- `config/chains.yaml`: RPCs, Multicall3, confirmations, reorg depth, log span, rate limits
- `config/protocols.yaml`: registry (chain, protocol, addresses, deployment block, liquidation type, access, ABI source, verification notes)
- `config/dex.yaml`: venues (every address checked on-chain via `factory()`), connector tokens
- `config/settings.yaml`: thresholds, slippage, safety buffer, simulation, alerts. Each key can be overridden with `LIQMON_<KEY>`.

Secrets only via env: `LIQMON_PRIVATE_KEY`, `LIQMON_TELEGRAM_BOT_TOKEN`, `LIQMON_TELEGRAM_CHAT_ID`,
`LIQMON_DISCORD_WEBHOOK_URL`. Executor contract per chain: `LIQMON_EXECUTOR_BSC`,
`LIQMON_EXECUTOR_AVALANCHE`. Private submission endpoint: `LIQMON_TX_RPC_<CHAIN>`.

## Safety model

1. The scanner never has key material. `dry_run: true` is the default.
2. `executor/broadcaster.py` broadcasts only when `dry_run` is false, `--broadcast` is given, a key is in the env,
   the deployed executor's `owner()` matches the key and its `pool()` matches the protocol, the opportunity
   reproduces at the latest block, and a final fork simulation of the exact transaction (real executor, real
   owner, on-chain `minProfit` = threshold + gas) succeeds.
3. The executor contract is owner-gated. The flash callback accepts only loans it initiated itself, and any
   shortfall reverts atomically.
4. A position is never "executable" on an off-chain estimate. Only a successful simulation counts.

## What the research found (read before expecting profit)

- Aave V3 on both chains runs **v3.7** (`POOL_REVISION` 11). Its close factor, dust rule, fee split and
  rounding differ from older V3 versions. The adapter reads the thresholds from the deployed library.
- On Avalanche Aave V3, every liquidation checked in a ~600k-block window **landed in the same block as the
  oracle update that triggered it**. At the end of the previous block, all 11 borrowers checked still had HF > 1.
  A block-polling scanner will usually only see these after they are gone. See [docs/protocols/aave_v3.md](docs/protocols/aave_v3.md).
- Lista DAO's Dutch auctions are **keeper-whitelisted on-chain** (`auctionWhitelistMode = 1`), contrary to
  its public docs. The system monitors them read-only.

More detail in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [docs/PROTOCOL_RESEARCH.md](docs/PROTOCOL_RESEARCH.md).
