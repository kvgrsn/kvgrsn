"""Dutch-auction math and a read-only reader for MakerDAO-style Clippers.

The price functions are exact integer ports of the abacus contracts
(LinearDecrease, StairstepExponentialDecrease, ExponentialDecrease) and of
Clipper.status(); the reader pulls every parameter (buf, tail, cusp, chip,
tip, calc and the calc's tau/step/cut) from the deployed contracts. Nothing
defaults to MakerDAO mainnet values.

Access: reading auction state is always fine. Whether you may *bid* depends
on the deployment. Lista DAO's BSC Clipper, for example, has `take`/`redo`
behind `auth`, reachable only through Interaction.buyFromAuction, which is
restricted to an auction whitelist (auctionWhitelistMode == 1, verified
on-chain). ``ClipperReader`` never sends transactions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..models import AuctionState
from ..rpc.abi import Fn
from ..rpc.client import RpcClient
from ..rpc.multicall import Call, Multicall

RAY = 10**27
WAD = 10**18


def rmul(x: int, y: int) -> int:
    return x * y // RAY


def rdiv(x: int, y: int) -> int:
    return x * RAY // y


def rpow(x: int, n: int, b: int = RAY) -> int:
    """Port of the abacus `rpow` (exponentiation by squaring, round half up)."""
    if x == 0:
        return b if n == 0 else 0
    z = x if n % 2 else b
    half = b // 2
    n //= 2
    while n:
        x = (x * x + half) // b
        if n % 2:
            z = (z * x + half) // b
        n //= 2
    return z


@dataclass(frozen=True)
class LinearDecrease:
    tau: int

    def price(self, top: int, dur: int) -> int:
        if dur >= self.tau:
            return 0
        return rmul(top, (self.tau - dur) * RAY // self.tau)

    def seconds_until_ratio(self, ratio_ray: int) -> int | None:
        """First `dur` with price/top < ratio (i.e. cusp trigger)."""
        # price/top = (tau - dur)/tau < ratio  <=>  dur > tau * (1 - ratio)
        return self.tau * (RAY - ratio_ray) // RAY + 1


@dataclass(frozen=True)
class StairstepExponentialDecrease:
    step: int
    cut: int

    def price(self, top: int, dur: int) -> int:
        return rmul(top, rpow(self.cut, dur // self.step, RAY))

    def seconds_until_ratio(self, ratio_ray: int) -> int | None:
        if self.cut >= RAY:
            return None
        steps, factor = 0, RAY
        while factor >= ratio_ray and steps < 100_000:
            steps += 1
            factor = rpow(self.cut, steps, RAY)
        return steps * self.step


@dataclass(frozen=True)
class ExponentialDecrease:
    cut: int

    def price(self, top: int, dur: int) -> int:
        return rmul(top, rpow(self.cut, dur, RAY))

    def seconds_until_ratio(self, ratio_ray: int) -> int | None:
        return StairstepExponentialDecrease(1, self.cut).seconds_until_ratio(ratio_ray)


Calc = LinearDecrease | StairstepExponentialDecrease | ExponentialDecrease


def clipper_status(calc: Calc, tic: int, top: int, now: int, tail: int, cusp: int) -> tuple[bool, int]:
    """Port of Clipper.status: (done/needsRedo, price)."""
    dur = now - tic
    price = calc.price(top, dur)
    done = dur > tail or rdiv(price, top) < cusp
    return done, price


@dataclass(frozen=True)
class ClipperParams:
    clipper: str
    buf: int
    tail: int
    cusp: int
    chip: int
    tip: int
    calc_address: str
    calc: Calc | None


# Clipper getters
_BUF = Fn("buf()", ["uint256"])
_TAIL = Fn("tail()", ["uint256"])
_CUSP = Fn("cusp()", ["uint256"])
_CHIP = Fn("chip()", ["uint64"])
_TIP = Fn("tip()", ["uint192"])
_CALC = Fn("calc()", ["address"])
_LIST = Fn("list()", ["uint256[]"])
_SALES = Fn("sales(uint256)", ["(uint256,uint256,uint256,address,uint96,uint256)"])
_GET_STATUS = Fn("getStatus(uint256)", ["bool", "uint256", "uint256", "uint256"])
# Abacus getters
_TAU = Fn("tau()", ["uint256"])
_STEP = Fn("step()", ["uint256"])
_CUT = Fn("cut()", ["uint256"])


class ClipperReader:
    def __init__(self, client: RpcClient, multicall: Multicall):
        self.client = client
        self.mc = multicall

    async def params(self, clipper: str, block: int | str = "latest") -> ClipperParams:
        r = await self.mc.run(
            [Call(clipper, f) for f in (_BUF, _TAIL, _CUSP, _CHIP, _TIP, _CALC)], block
        )
        if not all(x.success for x in (r[0], r[1], r[2], r[5])):
            raise RuntimeError(f"{clipper} does not expose Clipper getters (buf/tail/cusp/calc)")
        calc_addr = r[5].value
        c = await self.mc.run([Call(calc_addr, _TAU), Call(calc_addr, _STEP), Call(calc_addr, _CUT)], block)
        calc: Calc | None
        if c[0].success:
            calc = LinearDecrease(int(c[0].value))
        elif c[1].success and c[2].success:
            calc = StairstepExponentialDecrease(int(c[1].value), int(c[2].value))
        elif c[2].success:
            calc = ExponentialDecrease(int(c[2].value))
        else:
            calc = None  # unknown abacus; rely on the Clipper's own getStatus()
        return ClipperParams(
            clipper=clipper,
            buf=int(r[0].value),
            tail=int(r[1].value),
            cusp=int(r[2].value),
            chip=int(r[3].value) if r[3].success else 0,
            tip=int(r[4].value) if r[4].success else 0,
            calc_address=calc_addr,
            calc=calc,
        )

    async def active_auctions(
        self,
        clipper: str,
        *,
        protocol_id: str,
        collateral_asset: str,
        debt_asset: str,
        oracle_price_ray: int | None,
        block: int | str = "latest",
    ) -> list[AuctionState]:
        params = await self.params(clipper, block)
        ids_res = await self.mc.run([Call(clipper, _LIST)], block)
        ids = list(ids_res[0].value) if ids_res[0].success else []
        if not ids:
            return []
        blk = await self.client.get_block(block if isinstance(block, int) else "latest")
        now = int(blk["timestamp"], 16)
        calls = []
        for i in ids:
            calls += [Call(clipper, _SALES, (i,)), Call(clipper, _GET_STATUS, (i,))]
        res = await self.mc.run(calls, block)
        out = []
        for n, auction_id in enumerate(ids):
            sale, status = res[2 * n], res[2 * n + 1]
            if not (sale.success and status.success):
                continue
            _pos, tab, lot, usr, tic, top = sale.value
            needs_redo, price, _lot, _tab = status.value  # the Clipper's own view of the price
            state = describe_auction(
                params,
                auction_id=str(auction_id),
                tic=int(tic),
                top=int(top),
                lot=int(lot),
                tab=int(tab),
                now=now,
                onchain_price=int(price),
                onchain_needs_redo=bool(needs_redo),
                oracle_price_ray=oracle_price_ray,
                protocol_id=protocol_id,
                collateral_asset=collateral_asset,
                debt_asset=debt_asset,
            )
            state.params["usr"] = usr
            out.append(state)
        return out


def describe_auction(
    params: ClipperParams,
    *,
    auction_id: str,
    tic: int,
    top: int,
    lot: int,
    tab: int,
    now: int,
    onchain_price: int | None,
    onchain_needs_redo: bool | None,
    oracle_price_ray: int | None,
    protocol_id: str,
    collateral_asset: str,
    debt_asset: str,
) -> AuctionState:
    price = onchain_price
    needs_redo = onchain_needs_redo
    seconds_until_restart = None
    if params.calc is not None:
        local_done, local_price = clipper_status(params.calc, tic, top, now, params.tail, params.cusp)
        price = local_price if price is None else price
        needs_redo = local_done if needs_redo is None else needs_redo
        cusp_after = params.calc.seconds_until_ratio(params.cusp)
        limits = [params.tail + 1]
        if cusp_after is not None:
            limits.append(cusp_after)
        seconds_until_restart = max(0, min(limits) - (now - tic))
    discount = max_discount = None
    if oracle_price_ray and price is not None:
        discount = 1 - price / oracle_price_ray
        # Lowest price reachable before a restart is top * cusp.
        max_discount = 1 - rmul(top, params.cusp) / oracle_price_ray
    return AuctionState(
        protocol_id=protocol_id,
        auction_id=auction_id,
        collateral_asset=collateral_asset,
        debt_asset=debt_asset,
        lot=lot,
        tab=tab,
        start_time=tic,
        start_price=top,
        current_price=int(price or 0),
        oracle_price=oracle_price_ray,
        discount_vs_oracle=discount,
        max_discount=max_discount,
        needs_restart=bool(needs_redo),
        seconds_until_restart=seconds_until_restart,
        params={
            "buf": params.buf,
            "tail": params.tail,
            "cusp": params.cusp,
            "chip": params.chip,
            "tip": params.tip,
            "calc": params.calc_address,
            "calc_kind": type(params.calc).__name__ if params.calc else "unknown",
            "calc_params": params.calc.__dict__ if params.calc else {},
        },
    )


def params_summary(p: ClipperParams) -> dict[str, Any]:
    return {
        "clipper": p.clipper,
        "buf": p.buf / RAY,
        "tail_s": p.tail,
        "cusp": p.cusp / RAY,
        "chip": p.chip / WAD,
        "tip_rad": p.tip,
        "calc": p.calc_address,
        "calc_kind": type(p.calc).__name__ if p.calc else "unknown",
        "calc_params": p.calc.__dict__ if p.calc else {},
    }
