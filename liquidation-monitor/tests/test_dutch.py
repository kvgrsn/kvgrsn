from liqmon.auctions.dutch import (
    RAY,
    ClipperParams,
    ExponentialDecrease,
    LinearDecrease,
    StairstepExponentialDecrease,
    clipper_status,
    describe_auction,
    rpow,
)


def test_rpow_matches_reference_cases():
    assert rpow(0, 0) == RAY
    assert rpow(0, 5) == 0
    assert rpow(RAY, 10) == RAY
    assert rpow(2 * RAY, 3) == 8 * RAY
    half = RAY // 2
    assert rpow(half, 2) == RAY // 4
    # 0.99^2 = 0.9801
    assert rpow(99 * RAY // 100, 2) == 9801 * RAY // 10000


def test_linear_decrease():
    calc = LinearDecrease(tau=3600)
    top = 110 * RAY
    assert calc.price(top, 0) == top
    assert calc.price(top, 1800) == 55 * RAY
    assert calc.price(top, 3600) == 0
    assert calc.price(top, 5000) == 0


def test_stairstep_and_exponential():
    step = StairstepExponentialDecrease(step=90, cut=99 * RAY // 100)
    top = 100 * RAY
    assert step.price(top, 89) == top
    assert step.price(top, 90) == 99 * RAY
    exp = ExponentialDecrease(cut=99 * RAY // 100)
    assert exp.price(top, 1) == 99 * RAY


def test_clipper_status_cusp_and_tail():
    # Lista's BSC clipper parameters read on-chain: tail=1200, cusp=0.6, LinearDecrease tau=3600
    calc = LinearDecrease(tau=3600)
    top = 100 * RAY
    done, price = clipper_status(calc, tic=0, top=top, now=600, tail=1200, cusp=6 * RAY // 10)
    assert not done and price == rmul_(top, (3600 - 600) * RAY // 3600)
    done, _ = clipper_status(calc, tic=0, top=top, now=1201, tail=1200, cusp=6 * RAY // 10)
    assert done  # tail exceeded
    calc_fast = LinearDecrease(tau=1000)
    done, _ = clipper_status(calc_fast, tic=0, top=top, now=401, tail=10_000, cusp=6 * RAY // 10)
    assert done  # price/top < cusp


def rmul_(x, y):
    return x * y // RAY


def test_describe_auction_discount_and_restart_time():
    params = ClipperParams(
        clipper="0xclip",
        buf=11 * RAY // 10,
        tail=1200,
        cusp=6 * RAY // 10,
        chip=0,
        tip=0,
        calc_address="0xcalc",
        calc=LinearDecrease(tau=3600),
    )
    oracle = 100 * RAY
    top = 110 * RAY  # buf 1.1
    st = describe_auction(
        params,
        auction_id="7",
        tic=1_000,
        top=top,
        lot=10**18,
        tab=10**45,
        now=1_000 + 720,
        onchain_price=None,
        onchain_needs_redo=None,
        oracle_price_ray=oracle,
        protocol_id="x",
        collateral_asset="c",
        debt_asset="d",
    )
    # price after 720s = 110 * (2880/3600) = 88 -> 12% below oracle
    assert st.current_price == 88 * RAY
    assert abs(st.discount_vs_oracle - 0.12) < 1e-12
    # lowest price before restart = top * cusp = 66 -> 34% discount
    assert abs(st.max_discount - 0.34) < 1e-12
    # tail (1201s) triggers before cusp (1441s): 1201 - 720 = 481s left
    assert st.seconds_until_restart == 481
    assert not st.needs_restart
