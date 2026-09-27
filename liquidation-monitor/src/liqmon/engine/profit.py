"""Profitability engine.

expectedProfit = swapOut(collateral -> debt asset, executable quote)
               - slippage haircut
               - debt repaid
               - flash-loan fee
               - gas cost
               - safety buffer (fixed USD + bps of gross collateral value)

DEX fees are already inside an executable quote, and the protocol's
liquidation fee is already deducted from the collateral the protocol hands
over. Both are reported for transparency but not subtracted a second time.
A positive liquidation bonus alone never makes an opportunity profitable.
"""

from __future__ import annotations

from ..config import Settings
from ..models import FundingPlan, LiquidationQuote, ProfitEstimate, SwapQuote

# Pre-simulation gas guesses (replaced by the simulated gasUsed once available).
GAS_LIQUIDATION = 450_000
GAS_FLASH_OVERHEAD = 120_000
GAS_EXECUTOR_OVERHEAD = 60_000


def estimate_gas(quote: LiquidationQuote, swap: SwapQuote | None, funding: FundingPlan) -> int:
    gas = GAS_LIQUIDATION + GAS_EXECUTOR_OVERHEAD
    if funding.mode == "flash":
        gas += GAS_FLASH_OVERHEAD
    if swap is not None:
        gas += swap.gas_estimate
    return gas


def swap_fee_usd(swap: SwapQuote | None, amount_in_usd: float) -> float:
    if swap is None:
        return 0.0
    if swap.kind == "uniswap_v3":
        # fee tiers are in hundredths of a bip (1e-6); compound over hops
        remaining = 1.0
        for f in swap.fees:
            remaining *= 1 - f / 1_000_000
        return amount_in_usd * (1 - remaining)
    remaining = 1.0
    for f in swap.fees:
        remaining *= 1 - f / 10_000
    return amount_in_usd * (1 - remaining)


def estimate_profit(
    quote: LiquidationQuote,
    swap: SwapQuote | None,
    funding: FundingPlan,
    *,
    debt_price_usd: float,
    collateral_price_usd: float,
    native_price_usd: float,
    gas_price_wei: int,
    gas_units: int,
    settings: Settings,
) -> ProfitEstimate:
    same_asset = quote.collateral_asset.lower() == quote.debt_asset.lower()
    swap_out = quote.expected_collateral_out if same_asset else (swap.amount_out if swap else 0)
    slippage = 0 if same_asset else swap_out * settings.slippage_bps // 10_000
    flash_fee = funding.fee if funding.mode == "flash" else 0

    d_unit = 10**quote.debt_decimals
    c_unit = 10**quote.collateral_decimals
    to_usd_debt = lambda x: x * debt_price_usd / d_unit  # noqa: E731

    gross_collateral_usd = quote.expected_collateral_out * collateral_price_usd / c_unit
    debt_repaid_usd = to_usd_debt(quote.expected_debt_repaid)
    effective_gas_price = int(gas_price_wei * settings.gas_price_multiplier)
    gas_cost_native = gas_units * effective_gas_price / 1e18
    gas_cost_usd = gas_cost_native * native_price_usd
    safety = settings.safety_buffer_usd + gross_collateral_usd * settings.safety_buffer_bps / 10_000

    net_units = swap_out - slippage - quote.expected_debt_repaid - flash_fee
    expected_usd = to_usd_debt(net_units) - gas_cost_usd - safety
    return ProfitEstimate(
        debt_repaid=quote.expected_debt_repaid,
        collateral_received=quote.expected_collateral_out,
        swap_out=swap_out,
        flash_fee=flash_fee,
        slippage_buffer=slippage,
        gross_collateral_usd=gross_collateral_usd,
        debt_repaid_usd=debt_repaid_usd,
        swap_fee_usd=swap_fee_usd(None if same_asset else swap, gross_collateral_usd),
        protocol_fee_usd=quote.protocol_fee_collateral * collateral_price_usd / c_unit,
        flash_fee_usd=to_usd_debt(flash_fee),
        slippage_usd=to_usd_debt(slippage),
        gas_units=gas_units,
        gas_price_wei=effective_gas_price,
        gas_cost_native=gas_cost_native,
        gas_cost_usd=gas_cost_usd,
        safety_buffer_usd=safety,
        expected_profit_usd=expected_usd,
        expected_profit_native=expected_usd / native_price_usd if native_price_usd else 0.0,
        native_price_usd=native_price_usd,
    )


def usd_to_units(usd: float, price_usd: float, decimals: int) -> int:
    """Convert a USD amount into raw token units (0 if the price is unknown)."""
    if price_usd <= 0 or usd <= 0:
        return 0
    return int(usd / price_usd * 10**decimals)
