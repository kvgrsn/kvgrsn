"""Exact integer port of Aave v3.7 liquidation math.

Source: verified ``LiquidationLogic.sol`` of the Pool implementation deployed
on Avalanche (0x6cddFF90124bA51afac5715314db7C9546b32204, POOL_REVISION 11),
whose linked LiquidationLogic library (0x96D5686812e33Ab509ECCDb38C89d15607B2a413)
has identical runtime bytecode on BSC.

Rounding follows the Solidity helpers exactly (PercentageMath, MathUtils,
WadRayMath). The public constants (CLOSE_FACTOR_HF_THRESHOLD,
MIN_BASE_MAX_CLOSE_FACTOR_THRESHOLD, MIN_LEFTOVER_BASE) are read from the
deployed library at runtime and passed in; only
DEFAULT_LIQUIDATION_CLOSE_FACTOR is `internal` (not readable on-chain) and is
taken from the verified source.

This is used to *plan* a liquidation (which pair, how much debt to cover,
what to flash-borrow). The simulation is what confirms it.
"""

from __future__ import annotations

from dataclasses import dataclass

PERCENTAGE_FACTOR = 10_000
HALF_PERCENTAGE_FACTOR = 5_000
RAY = 10**27
WAD = 10**18
MAX_UINT256 = 2**256 - 1

# `uint256 internal constant DEFAULT_LIQUIDATION_CLOSE_FACTOR = 0.5e4;`
DEFAULT_LIQUIDATION_CLOSE_FACTOR = 5_000
# ValidationLogic: `uint256 public constant HEALTH_FACTOR_LIQUIDATION_THRESHOLD = 1e18;`
HEALTH_FACTOR_LIQUIDATION_THRESHOLD = WAD


# ---------------------------------------------------------------- helpers


def percent_mul(value: int, percentage: int) -> int:
    if percentage == 0:
        return 0
    return (value * percentage + HALF_PERCENTAGE_FACTOR) // PERCENTAGE_FACTOR


def percent_mul_floor(value: int, percentage: int) -> int:
    return (value * percentage) // PERCENTAGE_FACTOR


def percent_mul_ceil(value: int, percentage: int) -> int:
    product = value * percentage
    return product // PERCENTAGE_FACTOR + (1 if product % PERCENTAGE_FACTOR else 0)


def percent_div_floor(value: int, percentage: int) -> int:
    if percentage == 0:
        raise ZeroDivisionError("percentDivFloor by zero")
    return (value * PERCENTAGE_FACTOR) // percentage


def percent_div_ceil(value: int, percentage: int) -> int:
    if percentage == 0:
        raise ZeroDivisionError("percentDivCeil by zero")
    val = value * PERCENTAGE_FACTOR
    return val // percentage + (1 if val % percentage else 0)


def mul_div_ceil(a: int, b: int, c: int) -> int:
    if c == 0:
        raise ZeroDivisionError("mulDivCeil by zero")
    product = a * b
    return product // c + (1 if product % c else 0)


def ray_mul_floor(a: int, b: int) -> int:
    return (a * b) // RAY


def ray_mul_ceil(a: int, b: int) -> int:
    product = a * b
    return product // RAY + (1 if product % RAY else 0)


def ray_div_floor(a: int, b: int) -> int:
    return (a * RAY) // b


def ray_div_ceil(a: int, b: int) -> int:
    scaled = a * RAY
    return scaled // b + (1 if scaled % b else 0)


# ------------------------------------------------------------ core logic


@dataclass(frozen=True)
class LiquidationConstants:
    close_factor_hf_threshold: int          # read from LiquidationLogic
    min_base_max_close_factor_threshold: int  # read from LiquidationLogic
    min_leftover_base: int                  # read from LiquidationLogic
    default_close_factor_bps: int = DEFAULT_LIQUIDATION_CLOSE_FACTOR


@dataclass(frozen=True)
class LiquidationInputs:
    debt_to_cover: int
    borrower_collateral_balance: int   # aToken balance (underlying units)
    borrower_reserve_debt: int         # variable debt token balance (underlying units)
    collateral_price: int              # oracle price, base currency units
    debt_price: int
    collateral_decimals: int
    debt_decimals: int
    liquidation_bonus_bps: int         # e.g. 10500 = 5% bonus (e-mode bonus if applicable)
    liquidation_protocol_fee_bps: int  # share of the bonus paid to the treasury
    health_factor: int                 # wad
    total_debt_base: int               # getUserAccountData().totalDebtBase


@dataclass(frozen=True)
class LiquidationResult:
    max_liquidatable_debt: int
    actual_debt_to_liquidate: int
    collateral_to_liquidator: int
    protocol_fee: int
    close_factor_capped: bool
    dust_violation: bool
    liquidates_all_debt: bool
    liquidates_all_collateral: bool
    debt_leftover_base: int
    collateral_leftover_base: int


def calculate_available_collateral_to_liquidate(
    collateral_price: int,
    collateral_unit: int,
    debt_price: int,
    debt_unit: int,
    debt_to_cover: int,
    borrower_collateral_balance: int,
    liquidation_bonus: int,
    liquidation_protocol_fee_bps: int,
) -> tuple[int, int, int]:
    """Port of ``_calculateAvailableCollateralToLiquidate``.

    Returns (collateralAmount to liquidator, debtAmountNeeded, protocolFee).
    """
    base_collateral = (debt_price * debt_to_cover * collateral_unit) // (collateral_price * debt_unit)
    max_collateral_to_liquidate = percent_mul_floor(base_collateral, liquidation_bonus)

    if max_collateral_to_liquidate > borrower_collateral_balance:
        collateral_amount = borrower_collateral_balance
        debt_amount_needed = percent_div_ceil(
            (collateral_price * collateral_amount * debt_unit) // (debt_price * collateral_unit),
            liquidation_bonus,
        )
    else:
        collateral_amount = max_collateral_to_liquidate
        debt_amount_needed = debt_to_cover

    protocol_fee = 0
    if liquidation_protocol_fee_bps != 0:
        bonus_collateral = collateral_amount - percent_div_floor(collateral_amount, liquidation_bonus)
        protocol_fee = percent_mul_ceil(bonus_collateral, liquidation_protocol_fee_bps)
        collateral_amount -= protocol_fee
    return collateral_amount, debt_amount_needed, protocol_fee


def compute_liquidation(inp: LiquidationInputs, const: LiquidationConstants) -> LiquidationResult:
    """Port of the amount logic in ``executeLiquidationCall``.

    Validation (HF < 1, active/unpaused reserves, grace period, collateral
    enabled, non-zero debt, no self-liquidation) is checked by the adapter.
    """
    collateral_unit = 10**inp.collateral_decimals
    debt_unit = 10**inp.debt_decimals

    borrower_reserve_debt_base = mul_div_ceil(inp.borrower_reserve_debt, inp.debt_price, debt_unit)
    borrower_reserve_collateral_base = (inp.borrower_collateral_balance * inp.collateral_price) // collateral_unit

    max_liquidatable_debt = inp.borrower_reserve_debt
    close_factor_capped = False
    if (
        borrower_reserve_collateral_base >= const.min_base_max_close_factor_threshold
        and borrower_reserve_debt_base >= const.min_base_max_close_factor_threshold
        and inp.health_factor > const.close_factor_hf_threshold
    ):
        total_default_liquidatable_debt_base = percent_mul(inp.total_debt_base, const.default_close_factor_bps)
        if borrower_reserve_debt_base > total_default_liquidatable_debt_base:
            max_liquidatable_debt = (total_default_liquidatable_debt_base * debt_unit) // inp.debt_price
            close_factor_capped = True

    actual_debt = min(inp.debt_to_cover, max_liquidatable_debt)

    collateral_out, actual_debt, protocol_fee = calculate_available_collateral_to_liquidate(
        inp.collateral_price,
        collateral_unit,
        inp.debt_price,
        debt_unit,
        actual_debt,
        inp.borrower_collateral_balance,
        inp.liquidation_bonus_bps,
        inp.liquidation_protocol_fee_bps,
    )

    all_debt = actual_debt >= inp.borrower_reserve_debt
    all_collateral = collateral_out + protocol_fee >= inp.borrower_collateral_balance
    debt_leftover_base = 0
    collateral_leftover_base = 0
    dust = False
    if not all_debt and not all_collateral:
        debt_leftover_base = mul_div_ceil(inp.borrower_reserve_debt - actual_debt, inp.debt_price, debt_unit)
        collateral_leftover_base = (
            (inp.borrower_collateral_balance - collateral_out - protocol_fee) * inp.collateral_price
        ) // collateral_unit
        dust = not (
            debt_leftover_base >= const.min_leftover_base and collateral_leftover_base >= const.min_leftover_base
        )

    return LiquidationResult(
        max_liquidatable_debt=max_liquidatable_debt,
        actual_debt_to_liquidate=actual_debt,
        collateral_to_liquidator=collateral_out,
        protocol_fee=protocol_fee,
        close_factor_capped=close_factor_capped,
        dust_violation=dust,
        liquidates_all_debt=all_debt,
        liquidates_all_collateral=all_collateral,
        debt_leftover_base=debt_leftover_base,
        collateral_leftover_base=collateral_leftover_base,
    )


def plan_debt_to_cover(
    inp: LiquidationInputs,
    const: LiquidationConstants,
    leftover_margin_bps: int = 200,
) -> tuple[int, LiquidationResult] | None:
    """Choose the ``debtToCover`` argument for one (collateral, debt) pair.

    1. Try ``type(uint256).max``: the Pool clamps it to the close-factor cap
       and to what the collateral can pay for. Passing max (rather than the
       exact debt seen one block earlier) keeps a full liquidation full even
       after interest accrues, so it cannot trip MustNotLeaveDust.
    2. If that would leave dust, binary-search the largest amount that leaves
       at least MIN_LEFTOVER_BASE (+margin) of both debt and collateral.

    Returns None when no amount satisfies the dust rule.
    """
    first = compute_liquidation(_with_dtc(inp, MAX_UINT256), const)
    if not first.dust_violation and first.actual_debt_to_liquidate > 0:
        return MAX_UINT256, first

    strict = LiquidationConstants(
        close_factor_hf_threshold=const.close_factor_hf_threshold,
        min_base_max_close_factor_threshold=const.min_base_max_close_factor_threshold,
        min_leftover_base=const.min_leftover_base * (PERCENTAGE_FACTOR + leftover_margin_bps) // PERCENTAGE_FACTOR,
        default_close_factor_bps=const.default_close_factor_bps,
    )

    def ok(amount: int) -> LiquidationResult | None:
        res = compute_liquidation(_with_dtc(inp, amount), strict)
        return None if res.dust_violation or res.actual_debt_to_liquidate == 0 else res

    lo, hi = 0, first.actual_debt_to_liquidate
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if ok(mid):
            lo = mid
        else:
            hi = mid - 1
    if lo == 0:
        return None
    final = compute_liquidation(_with_dtc(inp, lo), const)
    if final.dust_violation:
        return None
    return lo, final


def _with_dtc(inp: LiquidationInputs, dtc: int) -> LiquidationInputs:
    return LiquidationInputs(
        debt_to_cover=dtc,
        borrower_collateral_balance=inp.borrower_collateral_balance,
        borrower_reserve_debt=inp.borrower_reserve_debt,
        collateral_price=inp.collateral_price,
        debt_price=inp.debt_price,
        collateral_decimals=inp.collateral_decimals,
        debt_decimals=inp.debt_decimals,
        liquidation_bonus_bps=inp.liquidation_bonus_bps,
        liquidation_protocol_fee_bps=inp.liquidation_protocol_fee_bps,
        health_factor=inp.health_factor,
        total_debt_base=inp.total_debt_base,
    )


def flash_premium(amount: int, premium_bps: int) -> int:
    """FlashLoanLogic.executeFlashLoanSimple: amount.percentMulCeil(premium)."""
    return percent_mul_ceil(amount, premium_bps)


# ------------------------------------------------------ config bitmaps


@dataclass(frozen=True)
class ReserveConfig:
    ltv: int
    liquidation_threshold: int
    liquidation_bonus: int
    decimals: int
    active: bool
    frozen: bool
    borrowing_enabled: bool
    paused: bool
    flashloan_enabled: bool
    reserve_factor: int
    liquidation_protocol_fee: int

    @classmethod
    def decode(cls, data: int) -> "ReserveConfig":
        """Bit layout from DataTypes.ReserveConfigurationMap (v3.7)."""
        return cls(
            ltv=data & 0xFFFF,
            liquidation_threshold=(data >> 16) & 0xFFFF,
            liquidation_bonus=(data >> 32) & 0xFFFF,
            decimals=(data >> 48) & 0xFF,
            active=bool((data >> 56) & 1),
            frozen=bool((data >> 57) & 1),
            borrowing_enabled=bool((data >> 58) & 1),
            paused=bool((data >> 60) & 1),
            flashloan_enabled=bool((data >> 63) & 1),
            reserve_factor=(data >> 64) & 0xFFFF,
            liquidation_protocol_fee=(data >> 152) & 0xFFFF,
        )


def user_is_borrowing(user_config: int, reserve_id: int) -> bool:
    return bool((user_config >> (reserve_id << 1)) & 1)


def user_is_using_as_collateral(user_config: int, reserve_id: int) -> bool:
    return bool((user_config >> ((reserve_id << 1) + 1)) & 1)


def emode_bitmap_has(bitmap: int, reserve_id: int) -> bool:
    return bool((bitmap >> reserve_id) & 1)
