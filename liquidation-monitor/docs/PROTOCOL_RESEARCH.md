# Protocol research: BSC and Avalanche C-Chain

Research date: 2026-09-27. Every row says how it was verified. Treat
anything not marked **on-chain** as a lead to verify, not a fact. Chain
deployments differ from Ethereum and from each other, and they get upgraded.

Verification legend:

- **on-chain**: read from the deployed contract in this session (`cast call` / `liqmon verify`)
- **verified source**: read from explorer-verified source of the deployed implementation
- **repo source**: read from the protocol's GitHub; bytecode not matched to the deployment
- **docs / 3rd party**: official docs or secondary sources only; must be verified before integration

## Summary table

| Chain | Protocol | Mechanism | Dutch auction? | Who can liquidate | Flash liquidity usable in the same tx | Status | Verification |
|---|---|---|---|---|---|---|---|
| BSC | **Aave V3** | repay-and-seize, HF<1, close factor 50%/100%, dust rule | no (fixed bonus) | **permissionless** (only self-liquidation blocked) | Aave `flashLoanSimple`, 5 bps | **implemented (reference)** | on-chain + verified source |
| AVAX | **Aave V3** | same as above (same v3.7 library bytecode) | no | **permissionless** | Aave `flashLoanSimple`, 5 bps; Balancer V2 Vault | **implemented (reference)** | on-chain + verified source |
| BSC | Venus Core Pool | Compound-style repay-and-seize, closeFactor 0.5 | no | **routed**: Comptroller only accepts the `Liquidator` contract; `Liquidator.liquidateBorrow` is public **unless the borrower is on `liquidationRestricted`**, then only allow-listed liquidators | not native in this research; use Aave/PancakeSwap V3 flash | next candidate | on-chain (closeFactor, liquidatorContract, treasuryPercent, VAI params) + repo source |
| BSC | Venus Isolated Pools | repay-and-seize; `liquidateAccount` needed below `minLiquidatableCollateral`; `healAccount` | no | permissionless per repo source; verify per pool | as above | candidate | docs / repo, verify |
| BSC | Lista DAO CDP (lisUSD) | MakerDAO-style Dog/Clipper **Dutch auction** (LinearDecrease) | **yes** | **keeper-restricted**: `Dog.bark`, `Clipper.take/redo` are `auth`; entry via `Interaction.startAuction/buyFromAuction` gated by `auctionWhitelisted`, and `auctionWhitelistMode == 1` on-chain | n/a | **do not execute**; read-only monitoring supported (`liqmon auctions`) | on-chain + repo source |
| BSC | Lista Lending (Moolah, Morpho-Blue fork) | per-market LIF repay-and-seize | no | docs describe a **liquidation whitelist** and a `PublicLiquidator`; access differs per market | Morpho-style free flash loans | candidate after per-market access check | docs / 3rd party |
| BSC | Kinza Finance | Aave V3 fork | no | likely permissionless | own pool flash | candidate; **do not reuse v3.7 rules without checking its version** | docs, verify |
| BSC | Avalon Finance | Aave V3 fork | no | likely permissionless | own pool flash | candidate, same caveat | docs, verify |
| BSC / AVAX | Euler V2 (EVK) | repay-and-seize with **health-based discount** (discount grows as HF falls, capped by `maxLiquidationDiscount`) | yes, Dutch-like in health rather than time | permissionless via EVC | EVK vault flash loans | candidate (large on AVAX) | docs / 3rd party, verify deployments |
| AVAX | Benqi Lending (Core) | Compound-style repay-and-seize | no | appears direct (`liquidatorContract()` does not exist on the Comptroller) | none native; use Aave/Balancer | candidate | on-chain (closeFactor 0.5, incentive 1.1) |
| AVAX | Silo V2 | isolated markets, `liquidationCall`, fixed liquidation fee | no | permissionless per docs | ERC-3156 flash loans in silos | candidate | docs, verify |
| BSC | Alpaca Finance | leveraged-farm `kill` / AF2 money market | no | whitelisted killers/liquidators per docs | n/a | skip unless access verified open | docs, verify |
| AVAX | DeltaPrime | prime-account liquidation | no | whitelisted liquidators | n/a | skip (restricted; exploited 2024/2025) | docs |
| BSC | Radiant Capital | Aave V2 fork | no | n/a | n/a | skip (exploited Oct 2024, markets paused) | 3rd party |
| AVAX | Banker Joe | Compound fork | no | n/a | n/a | skip (deprecated) | 3rd party |

### Dutch auctions (question 8)

- **Lista DAO CDP (BSC)** is the only MakerDAO-Clipper-style Dutch auction
  found on these chains. Parameters read on-chain from Clipper
  `0x2dcFb02CE33955b6Cc0aF34033189DE3ac4C0292` (ilk `ceABNBc`):
  `buf = 1.10`, `tail = 1200 s`, `cusp = 0.60`, `chip = 0`,
  `tip = 5 lisUSD` (rad), `calc = 0xBaf8…Cc03`, which is LinearDecrease with `tau = 3600`.
  `kicks = 864`, `count = 0` at the time of reading. It is **keeper-whitelisted**
  (below), so the system only monitors it.
- **Euler V2** uses a health-based discount: a Dutch auction indexed by health factor instead of time.
- Everything else surveyed uses fixed-bonus repay-and-seize.

### Permissionless (question 9)

On-chain and source evidence supports permissionless execution for Aave V3 (both
chains) and Venus Core Pool (via the public `Liquidator`, subject to its per-borrower
restriction). Benqi, Euler V2, Silo V2 and the Aave forks are probably
permissionless, but that still has to be confirmed per deployment. Lista CDP is
confirmed keeper-restricted. Lista Lending, Alpaca and DeltaPrime document whitelists.

### Flash liquidity (question 10)

| Chain | Source | Fee | Notes |
|---|---|---|---|
| both | Aave V3 `flashLoanSimple` | `FLASHLOAN_PREMIUM_TOTAL` = **5 bps** (on-chain), `percentMulCeil` | limited by reserve `virtualUnderlyingBalance` and the per-reserve flash-loan flag; **implemented** |
| AVAX | Balancer V2 Vault `0xBA12…2C8` | read `ProtocolFeesCollector.getFlashLoanFeePercentage()` | code present on-chain; not wired yet |
| BSC | Balancer V2 Vault address | n/a | code is present at the canonical address (24,512 bytes); liquidity/official status unverified |
| both | Uniswap V3 / PancakeSwap V3 pool `flash()` | pool fee tier | callback-based; the executor needs a callback per venue |
| both | V2-style flash swaps (PancakeSwap V2, Trader Joe V1, Pangolin) | 0.25–0.30% | usually worse than Aave |
| BSC | DODO V2 pool `flashLoan` | often 0, per pool | verify per pool |
| BSC | Lista Moolah `flashLoan` | 0 per docs | verify |

## Venus details (on-chain, BSC)

- Unitroller/Diamond Comptroller `0xfD36E2c2a6789Db23113685031d7F16329158384`:
  `closeFactorMantissa = 0.5e18`; `liquidatorContract = 0x0870793286aaDA55D39CE7f82fb2766e8004cF43`.
  The global `liquidationIncentiveMantissa()` selector **does not exist** on the
  current Diamond ("Diamond: Function does not exist"). The Liquidator source
  uses `comptroller.getEffectiveLiquidationIncentive(borrower, vTokenCollateral)`,
  so incentives are per market and per borrower. An adapter must read them
  that way, not from the old Compound getter.
- Liquidator: `treasuryPercentMantissa = 0.5e18`, i.e. **50 % of the bonus portion**
  of seized collateral goes to the protocol (repo source `_splitLiquidationIncentive`).
  `minLiquidatableVAI = 1000 VAI`, `forceVAILiquidate = false`.
  `checkRestrictions(borrower, msg.sender)` enforces `liquidationRestricted` /
  `allowedLiquidatorsByAccount`. Report restricted borrowers; never try to bypass.

## Lista DAO details (on-chain, BSC)

- `Interaction` proxy `0xB68443Ee3e828baD1526b3e0Bdf2Dfc6b1975ec4`: `auctionWhitelistMode() = 1`,
  `whitelistMode() = 1`; `auctionWhitelist(random) = 0`.
- Repo source: `modifier auctionWhitelisted { if (auctionWhitelistMode == 1) require(auctionWhitelist[msg.sender] == 1, ...) }`
  guards both `startAuction` and `buyFromAuction`.
- Lista's `dog.bark` and `clip.take/redo/kick` are all `auth` (unlike MakerDAO, where `bark`
  and `take` are public). The Interaction contract is a ward of the Clipper.
- The public docs page ("any Lista user can do it") **contradicts the deployed configuration**.
  The adapter must follow the chain.

## Aave V3 (reference): see [protocols/aave_v3.md](protocols/aave_v3.md)

## Research procedure for the next protocol

1. Official docs and addresses; cross-check against an address book or registry.
2. Resolve proxies (EIP-1967 slot) and fetch **verified source of the implementation**
   (Routescan works without a key for Avalanche; Sourcify for both; BscScan needs a key).
3. Read the liquidation entry point, its modifiers, and any router or "liquidator" contract
   the core contract requires.
4. Read the oracle path the liquidation uses (not a UI oracle).
5. Derive the exact formula including rounding; port it and unit-test it.
6. Identify keeper/allowlist/borrower-restriction logic and read its **current on-chain state**.
7. Identify discovery events and the deployment block.
8. For auctions: read every parameter (`buf/tail/cusp/chip/tip/calc` plus the calc's own params).
9. Enumerate collateral/debt assets from the deployment, not from docs.
10. Validate with `liqmon replay` against a real historical liquidation before trusting it.
