# Aave V3 on BSC and Avalanche: research record

## Deployments (on-chain, 2026-09-27)

| | BSC | Avalanche |
|---|---|---|
| PoolAddressesProvider | `0xff75B6da14FfbbfD355Daf7a2731456b3562Ba6D` | `0xa97684ead0e402dC232d5A977953DF7ECBaB3CDb` |
| Pool (proxy) | `0x6807dc923806fE8Fd134338EABCA509979a7e0cB` | `0x794a61358D6845594F94dc1DB02A252b5b4814aD` |
| Pool implementation (EIP-1967 slot) | `0x5e2B0FcC5b9734C7Ec0A03401ee9e6805F783B6d` | `0x6cddFF90124bA51afac5715314db7C9546b32204` |
| `POOL_REVISION()` | 11 | 11 |
| Oracle | `0x39bc1bfDa2130d6Bb6DBEfd366939b4c7aa7C697` | `0xEBd36016B3eD09D4693Ed4251c67Bd858c3c7C9C` |
| Oracle base | USD, `BASE_CURRENCY_UNIT = 1e8` | USD, `1e8` |
| `FLASHLOAN_PREMIUM_TOTAL` | 5 bps | 5 bps |
| LiquidationLogic library | `0x96D5686812e33Ab509ECCDb38C89d15607B2a413` | same address |
| Deployment block | unknown (needs archive/explorer key) | 11,970,506 |

Cross-checked against `bgd-labs/aave-address-book`. The LiquidationLogic and
FlashLoanLogic runtime bytecode is identical on both chains (masking the
library's own address prefix), so the verified Avalanche source applies to BSC.
The implementation is `aave-v3-origin` **v3.7**.

## Liquidation rule (verified source, v3.7)

Entry point: `Pool.liquidationCall(collateral, debt, borrower, debtToCover, receiveAToken)`, public.

Validation (`ValidationLogic.validateLiquidationCall`):

- `borrower != liquidator` (`SelfLiquidation`): the only caller restriction.
- Both reserves active and not paused.
- Both reserves past `liquidationGracePeriodUntil`.
- `healthFactor < 1e18`, computed by `GenericLogic.calculateUserAccountData`.
  The scanner uses `getUserAccountData`, which is the same computation.
- Collateral enabled as collateral by the borrower; borrower has debt in the debt reserve.

Amounts (`LiquidationLogic.executeLiquidationCall`), ported to `protocols/aave_v3/math.py`:

- `maxLiquidatableDebt = reserveDebt`, reduced to `50% × totalDebtBase` (converted)
  **only if** reserve collateral ≥ $2000, reserve debt ≥ $2000 **and** HF > 0.95.
  These constants are `public` on the library and read at runtime; only
  `DEFAULT_LIQUIDATION_CLOSE_FACTOR = 0.5e4` is `internal`.
- `baseCollateral = debtPrice × debt × colUnit / (colPrice × debtUnit)`;
  `maxCollateral = baseCollateral.percentMulFloor(bonus)`; if that exceeds the
  borrower's balance, take all of it and recompute
  `debtNeeded = (...).percentDivCeil(bonus)`.
- Protocol fee: `bonusCollateral = collateral − collateral.percentDivFloor(bonus)`;
  `fee = bonusCollateral.percentMulCeil(liquidationProtocolFee)`; the liquidator gets `collateral − fee`.
- Dust rule: unless all debt or all collateral of the pair is consumed, both leftovers
  must be ≥ `MIN_LEFTOVER_BASE` ($1000), otherwise `MustNotLeaveDust`.
- Bonus: the e-mode category's `liquidationBonus` if the borrower's e-mode includes the collateral
  (collateral bitmap), otherwise the reserve's.
- With `receiveAToken = false` the underlying is withdrawn, so the collateral reserve's
  `virtualUnderlyingBalance` must cover it.
- If no collateral remains, the remaining debt becomes reserve **deficit** (bad debt is burned).
  This does not change what the liquidator pays or receives.

`debtToCover` planning: pass `type(uint256).max` whenever that is dust-safe. The Pool
clamps it, and a full liquidation stays full even after interest accrues between
quote and inclusion. Otherwise binary-search the largest dust-safe amount with a 2% margin.

## Discovery

`Borrow(reserve indexed, user, onBehalfOf indexed, amount, interestRateMode, borrowRate, referralCode indexed)`:
`onBehalfOf` (topic 2) owns the debt. Every debt-creating path emits it (borrow, credit
delegation, flash loan left open as debt). Positions are dropped as `closed` once
`totalDebtBase == 0` and reactivated by a new Borrow event.

## Flash liquidity

`flashLoanSimple(receiver, asset, amount, params, 0)`: premium `amount.percentMulCeil(5)`.
Requires the reserve's flash-loan flag (bit 63), active and unpaused, and
`aToken.totalSupply() ≥ amount`. The virtual balance is decremented, so it bounds the amount.
The Pool has no reentrancy lock between `flashLoanSimple` and `liquidationCall`,
and the fork simulation exercises that path.

## Observed competition (Avalanche, ~600k blocks before 96.25M)

27 `LiquidationCall` events. For 11 of the larger ones the borrower's HF at the **end of
the previous block** was > 1 (1.0007 to 1.0079). The liquidation landed in the same block
as the oracle update, right after it (e.g. block 95,757,847: price update at tx 6,
liquidation at tx 7). A scanner that reads state once per block will see these positions
only after someone else has liquidated them. Competing for them needs oracle-update
(mempool/transmit) back-running, which is outside this project's scope. The realistic
targets for this system are positions nobody else takes: long-tail assets, small positions
below bots' gas thresholds, and moments when the fast bots are down.
