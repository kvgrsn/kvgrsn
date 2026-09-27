"""Aave v3.7 liquidation math: rounding helpers, close factor, dust rule, fee split."""

from liqmon.protocols.aave_v3.math import (
    MAX_UINT256,
    LiquidationConstants,
    LiquidationInputs,
    ReserveConfig,
    calculate_available_collateral_to_liquidate,
    compute_liquidation,
    emode_bitmap_has,
    flash_premium,
    mul_div_ceil,
    percent_div_ceil,
    percent_div_floor,
    percent_mul,
    percent_mul_ceil,
    percent_mul_floor,
    plan_debt_to_cover,
    ray_div_ceil,
    ray_mul_floor,
    user_is_borrowing,
    user_is_using_as_collateral,
)

# Values read from the deployed LiquidationLogic library (both chains).
CONST = LiquidationConstants(
    close_factor_hf_threshold=950_000_000_000_000_000,
    min_base_max_close_factor_threshold=2000 * 10**8,
    min_leftover_base=1000 * 10**8,
)
USD = 10**8  # oracle base unit


def inputs(**kw) -> LiquidationInputs:
    base = dict(
        debt_to_cover=MAX_UINT256,
        borrower_collateral_balance=10 * 10**18,  # 10 ETH-like
        borrower_reserve_debt=15_000 * 10**6,  # 15k USDC-like
        collateral_price=2000 * USD,
        debt_price=1 * USD,
        collateral_decimals=18,
        debt_decimals=6,
        liquidation_bonus_bps=10_500,
        liquidation_protocol_fee_bps=1000,
        health_factor=int(0.97e18),
        total_debt_base=15_000 * USD,
    )
    base.update(kw)
    return LiquidationInputs(**base)


def test_rounding_helpers_match_solidity():
    assert percent_mul(10_001, 5000) == 5001  # half-up
    assert percent_mul(3, 5000) == 2  # (15000+5000)//10000
    assert percent_mul_floor(3, 5000) == 1
    assert percent_mul_ceil(3, 5000) == 2
    assert percent_mul_ceil(2, 5000) == 1
    assert percent_div_floor(105, 10500) == 100
    assert percent_div_ceil(101, 10500) == 97  # 1010000/10500 = 96.19 -> 97
    assert mul_div_ceil(10, 3, 4) == 8
    assert mul_div_ceil(8, 1, 4) == 2
    assert ray_mul_floor(3, 10**27 // 2) == 1
    assert ray_div_ceil(1, 3 * 10**27) == 1
    assert percent_mul(0, 0) == 0


def test_flash_premium_ceil():
    # FlashLoanLogic: amount.percentMulCeil(premium); 5 bps
    assert flash_premium(1_000_000, 5) == 500
    assert flash_premium(1, 5) == 1  # rounds up
    assert flash_premium(0, 5) == 0


def test_close_factor_50pct_when_hf_above_threshold():
    r = compute_liquidation(inputs(), CONST)
    assert r.close_factor_capped
    # 50% of total debt base (15k) -> 7.5k USDC
    assert r.max_liquidatable_debt == 7_500 * 10**6
    assert r.actual_debt_to_liquidate == 7_500 * 10**6
    # base collateral = 7500/2000 = 3.75 ETH, *1.05 = 3.9375, minus 10% of bonus as fee
    base = 3_750_000_000_000_000_000
    with_bonus = base * 10_500 // 10_000
    bonus = with_bonus - with_bonus * 10_000 // 10_500
    fee = -(-bonus * 1000 // 10_000)
    assert r.protocol_fee == fee
    assert r.collateral_to_liquidator == with_bonus - fee
    assert not r.dust_violation


def test_full_close_factor_when_hf_at_or_below_095():
    r = compute_liquidation(inputs(health_factor=int(0.95e18)), CONST)
    assert not r.close_factor_capped
    assert r.max_liquidatable_debt == 15_000 * 10**6
    assert r.liquidates_all_debt


def test_full_close_factor_for_small_positions():
    # debt below MIN_BASE_MAX_CLOSE_FACTOR_THRESHOLD ($2000) -> 100%
    r = compute_liquidation(
        inputs(borrower_reserve_debt=1_500 * 10**6, total_debt_base=1_500 * USD, borrower_collateral_balance=10**18),
        CONST,
    )
    assert not r.close_factor_capped
    assert r.actual_debt_to_liquidate == 1_500 * 10**6


def test_collateral_capped_recomputes_debt_needed():
    # Collateral worth less than debt*bonus -> all collateral taken, debt reduced (percentDivCeil)
    r = compute_liquidation(
        inputs(
            borrower_collateral_balance=10**18,  # $2000
            borrower_reserve_debt=2_500 * 10**6,
            total_debt_base=2_500 * USD,
            health_factor=int(0.8e18),
        ),
        CONST,
    )
    assert r.liquidates_all_collateral
    expected_debt = percent_div_ceil(2000 * 10**6, 10_500)
    assert r.actual_debt_to_liquidate == expected_debt
    assert r.collateral_to_liquidator + r.protocol_fee == 10**18


def test_available_collateral_without_fee():
    col, debt, fee = calculate_available_collateral_to_liquidate(
        2000 * USD, 10**18, 1 * USD, 10**6, 1000 * 10**6, 10**19, 10_500, 0
    )
    assert fee == 0
    assert debt == 1000 * 10**6
    assert col == (5 * 10**17) * 10_500 // 10_000


def test_dust_rule_detected_and_planner_reduces_amount():
    # Reserve debt $2,000 of total $2,000, HF 0.97 -> 50% close factor ($1,000).
    # Seizing $1,050 of the $2,040 collateral leaves $990 < MIN_LEFTOVER_BASE
    # -> MustNotLeaveDust when passing type(uint256).max.
    inp = inputs(
        borrower_collateral_balance=1_020_000_000_000_000_000,  # 1.02 ETH = $2,040
        borrower_reserve_debt=2_000 * 10**6,
        total_debt_base=2_000 * USD,
        health_factor=int(0.97e18),
        collateral_price=2000 * USD,
    )
    at_max = compute_liquidation(inp, CONST)
    assert at_max.close_factor_capped
    assert at_max.dust_violation
    planned = plan_debt_to_cover(inp, CONST)
    assert planned is not None
    dtc, res = planned
    assert dtc != MAX_UINT256
    assert not res.dust_violation
    assert res.collateral_leftover_base >= CONST.min_leftover_base
    assert res.debt_leftover_base >= CONST.min_leftover_base


def test_planner_prefers_max_for_full_liquidation():
    dtc, res = plan_debt_to_cover(inputs(health_factor=int(0.9e18)), CONST)
    assert dtc == MAX_UINT256
    assert res.liquidates_all_debt


def test_reserve_config_decode_layout():
    data = (
        7500
        | (7800 << 16)
        | (10500 << 32)
        | (6 << 48)
        | (1 << 56)
        | (0 << 57)
        | (1 << 58)
        | (1 << 60)
        | (1 << 63)
        | (1000 << 64)
        | (1000 << 152)
    )
    cfg = ReserveConfig.decode(data)
    assert (cfg.ltv, cfg.liquidation_threshold, cfg.liquidation_bonus, cfg.decimals) == (7500, 7800, 10500, 6)
    assert cfg.active and cfg.borrowing_enabled and cfg.paused and cfg.flashloan_enabled and not cfg.frozen
    assert cfg.reserve_factor == 1000 and cfg.liquidation_protocol_fee == 1000


def test_user_config_bits():
    cfg = 0
    cfg |= 1 << (3 * 2)  # borrowing reserve 3
    cfg |= 1 << (5 * 2 + 1)  # collateral reserve 5
    assert user_is_borrowing(cfg, 3) and not user_is_using_as_collateral(cfg, 3)
    assert user_is_using_as_collateral(cfg, 5) and not user_is_borrowing(cfg, 5)
    assert emode_bitmap_has(0b1010, 1) and not emode_bitmap_has(0b1010, 2)
