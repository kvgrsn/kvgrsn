from liqmon.config import load_settings
from liqmon.dex.router import encode_v2_path, encode_v3_path
from liqmon.engine.profit import estimate_profit, swap_fee_usd, usd_to_units
from liqmon.flash.sources import FlashQuote, FlashSource, choose_funding
from liqmon.models import FundingPlan, LiquidationQuote, LiquidationType, SwapQuote
from liqmon.rpc.abi import decode_revert, register_errors, selector

A = "0x" + "aa" * 20
B = "0x" + "bb" * 20
C = "0x" + "cc" * 20


def quote(**kw):
    base = dict(
        protocol_id="p", chain="avalanche", account=C, block_number=1,
        liquidation_type=LiquidationType.REPAY_AND_SEIZE,
        collateral_asset=A, collateral_symbol="COL", collateral_decimals=18,
        debt_asset=B, debt_symbol="USD", debt_decimals=6,
        debt_to_cover_param=2**256 - 1, expected_debt_repaid=1_000 * 10**6,
        expected_collateral_out=int(1.045 * 10**18), protocol_fee_collateral=5 * 10**15,
        liquidation_bonus_bps=10_500, close_factor_capped=False, full_liquidation=True,
        debt_value_usd=1000.0, collateral_value_usd=1045.0,
    )
    base.update(kw)
    return LiquidationQuote(**base)


def swap(out):
    return SwapQuote("uniswap_v3", "uniswap_v3", A, [A, B], [500], int(1.045e18), out, 120_000, 3.0, "0x")


def test_profit_subtracts_every_cost_once():
    s = load_settings()
    q = quote()
    f = FundingPlan("flash", "aave_v3", B, 1_001 * 10**6, 500_500)
    est = estimate_profit(q, swap(1_040 * 10**6), f, debt_price_usd=1.0, collateral_price_usd=1000.0,
                          native_price_usd=20.0, gas_price_wei=25 * 10**9, gas_units=800_000, settings=s)
    slippage = 1_040 * 10**6 * s.slippage_bps // 10_000
    net_units = 1_040 * 10**6 - slippage - 1_000 * 10**6 - 500_500
    gas_usd = 800_000 * int(25e9 * s.gas_price_multiplier) / 1e18 * 20.0
    safety = s.safety_buffer_usd + 1045.0 * s.safety_buffer_bps / 10_000
    assert abs(est.expected_profit_usd - (net_units / 1e6 - gas_usd - safety)) < 1e-9
    # DEX fee is informational only (already inside the quote)
    assert est.swap_fee_usd > 0


def test_bonus_alone_is_not_profit():
    s = load_settings()
    q = quote()
    f = FundingPlan("flash", "aave_v3", B, 1_001 * 10**6, 500_500)
    # terrible exit: swap returns less than the debt
    est = estimate_profit(q, swap(990 * 10**6), f, debt_price_usd=1.0, collateral_price_usd=1000.0,
                          native_price_usd=20.0, gas_price_wei=25 * 10**9, gas_units=800_000, settings=s)
    assert q.gross_bonus_usd > 0 and est.expected_profit_usd < 0


def test_same_asset_needs_no_swap():
    s = load_settings()
    q = quote(collateral_asset=B, collateral_decimals=6, expected_collateral_out=1_045 * 10**6)
    est = estimate_profit(q, None, FundingPlan("wallet", "wallet", B, 1_001 * 10**6, 0), debt_price_usd=1.0,
                          collateral_price_usd=1.0, native_price_usd=20.0, gas_price_wei=0, gas_units=0, settings=s)
    assert est.swap_out == 1_045 * 10**6 and est.slippage_buffer == 0 and est.flash_fee == 0


def test_path_encoding():
    p = encode_v3_path([A, B, C], [500, 3000])
    assert len(p) == 20 + 3 + 20 + 3 + 20
    assert p[20:23] == (500).to_bytes(3, "big")
    assert p[43:46] == (3000).to_bytes(3, "big")
    assert encode_v2_path([A, B])[:32] == (32).to_bytes(32, "big")


def test_swap_fee_compounds():
    q = SwapQuote("v", "uniswap_v3", A, [A, B, C], [3000, 3000], 1, 1, 1, None, "0x")
    assert abs(swap_fee_usd(q, 1000.0) - 1000 * (1 - 0.997**2)) < 1e-9


class _Src(FlashSource):
    def __init__(self, name, fee, available):
        self.name, self._fee, self._available = name, fee, available

    def quote(self, asset, amount):
        if amount > self._available:
            return None
        return FlashQuote(self.name, asset, amount, self._fee, self._available)


def test_funding_prefers_wallet_then_cheapest_flash():
    srcs = [_Src("a", 50, 10**9), _Src("b", 10, 10**9), _Src("small", 0, 10)]
    assert choose_funding(B, 1000, 5000, srcs).mode == "wallet"
    plan = choose_funding(B, 1000, 10, srcs)
    assert plan.mode == "flash" and plan.source == "b"
    assert choose_funding(B, 10**10, None, srcs) is None


def test_usd_to_units():
    assert usd_to_units(25.0, 1.0, 6) == 25_000_000
    assert usd_to_units(10.0, 0.0, 18) == 0


def test_custom_error_decoding():
    import liqmon.protocols.aave_v3.abi  # noqa: F401 - registers errors

    assert decode_revert("0x" + selector("MustNotLeaveDust()").hex()) == "MustNotLeaveDust()"
    register_errors(["Foo(uint256)"])
    data = "0x" + selector("Foo(uint256)").hex() + (7).to_bytes(32, "big").hex()
    assert decode_revert(data) == "Foo(7)"
    err = "0x08c379a0" + (32).to_bytes(32, "big").hex() + (3).to_bytes(32, "big").hex() + b"abc".ljust(32, b"\0").hex()
    assert decode_revert(err) == "abc"


def test_restrict_dex():
    from liqmon.config import load_dex
    from liqmon.dex.router import restrict_dex

    dex = load_dex()["avalanche"]
    r = restrict_dex(dex, ["uniswap_v3"], ["USDC"])
    assert [v.name for v in r.venues] == ["uniswap_v3"] and list(r.connectors) == ["USDC"]
    assert restrict_dex(dex, None, []).connectors == {}
    assert restrict_dex(dex, None, None) == dex
