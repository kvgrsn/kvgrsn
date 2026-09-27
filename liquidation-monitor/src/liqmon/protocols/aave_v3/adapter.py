"""Aave V3 reference adapter (BSC + Avalanche).

Liquidation mechanism (verified against the deployed v3.7 source):
* ``Pool.liquidationCall(collateral, debt, borrower, debtToCover, receiveAToken)``
  is public. The only caller restriction is ``borrower != liquidator``
  (``SelfLiquidation``). No keeper whitelist exists.
* Condition: ``healthFactor < 1e18`` using the Pool's own
  ``GenericLogic.calculateUserAccountData`` (read via getUserAccountData).
* Both reserves must be active and unpaused, past their liquidation grace
  period, the collateral must be enabled as collateral by the borrower, and
  the borrower must hold debt in the debt reserve.
* Close factor 50% while HF > CLOSE_FACTOR_HF_THRESHOLD and the reserve
  position is >= MIN_BASE_MAX_CLOSE_FACTOR_THRESHOLD in both collateral and
  debt; otherwise 100%. Partial liquidations must leave >= MIN_LEFTOVER_BASE
  of both debt and collateral (MustNotLeaveDust).
* Bonus = e-mode category bonus if the borrower's e-mode includes the
  collateral, else the reserve bonus. A protocol fee share of the bonus goes
  to the treasury.
* Flash liquidity: ``Pool.flashLoanSimple`` with premium
  FLASHLOAN_PREMIUM_TOTAL (percentMulCeil), limited by the reserve's virtual
  underlying balance and the reserve's flash-loan flag.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Sequence

from eth_utils import to_checksum_address

from ...config import ProtocolSpec
from ...indexer.event_indexer import EventSpec
from ...models import (
    AccessPolicy,
    AssetPosition,
    Eligibility,
    FundingPlan,
    LiquidationQuote,
    LiquidationType,
    PositionState,
    SimStatus,
    SimulationResult,
    SwapQuote,
)
from ...rpc.abi import ERC20_SYMBOL, ERC20_SYMBOL_BYTES32, ERC20_BALANCE_OF, topic_to_address
from ...rpc.multicall import Call
from ...flash.sources import FlashQuote, FlashSource, choose_funding
from ..base import AdapterContext, ExecutionPlan, ProtocolAdapter
from . import abi as A
from .math import (
    HEALTH_FACTOR_LIQUIDATION_THRESHOLD,
    MAX_UINT256,
    LiquidationConstants,
    LiquidationInputs,
    ReserveConfig,
    emode_bitmap_has,
    flash_premium,
    plan_debt_to_cover,
    user_is_borrowing,
    user_is_using_as_collateral,
)

log = logging.getLogger(__name__)

SWAP_KIND = {None: 0, "uniswap_v3": 1, "uniswap_v2": 2}


@dataclass
class Reserve:
    asset: str
    symbol: str
    id: int
    a_token: str
    v_token: str
    config: ReserveConfig
    virtual_balance: int = 0
    grace_until: int = 0

    @property
    def decimals(self) -> int:
        return self.config.decimals


@dataclass
class EMode:
    id: int
    ltv: int
    liquidation_threshold: int
    liquidation_bonus: int
    collateral_bitmap: int


@dataclass
class MarketSnapshot:
    block: int
    prices: dict[str, int] = field(default_factory=dict)


class AaveV3FlashSource(FlashSource):
    """Pool.flashLoanSimple: premium = amount.percentMulCeil(FLASHLOAN_PREMIUM_TOTAL);
    requires the reserve's flash-loan flag, active and unpaused, and enough
    virtual underlying balance."""

    name = "aave_v3"

    def __init__(self, adapter: "AaveV3Adapter"):
        self.adapter = adapter

    def quote(self, asset: str, amount: int) -> FlashQuote | None:
        r = self.adapter.reserves.get(asset)
        if r is None or not (r.config.flashloan_enabled and r.config.active and not r.config.paused):
            return None
        if r.virtual_balance < amount:
            return None
        return FlashQuote(self.name, asset, amount, flash_premium(amount, self.adapter.flash_premium_bps), r.virtual_balance)


class AaveV3Adapter(ProtocolAdapter):
    liquidation_type = LiquidationType.REPAY_AND_SEIZE
    access_policy = AccessPolicy.PERMISSIONLESS

    def __init__(self, ctx: AdapterContext, spec: ProtocolSpec):
        super().__init__(ctx, spec)
        self.provider = spec.addresses["pool_addresses_provider"]
        self.pool: str = ""
        self.oracle: str = ""
        self.data_provider: str = ""
        self.market_id: str = ""
        self.pool_revision: int = 0
        self.flash_premium_bps: int = 0
        self.liquidation_logic: str = ""
        self.constants: LiquidationConstants | None = None
        self.base_currency_unit: int = 0
        self.reserves: dict[str, Reserve] = {}
        self.reserves_by_id: dict[int, Reserve] = {}
        self.emodes: dict[int, EMode] = {}
        self.snapshot = MarketSnapshot(block=0)

    # ------------------------------------------------------------ lifecycle

    async def load(self, block: int | str = "latest") -> None:
        mc = self.ctx.multicall
        res = await mc.run(
            [
                Call(self.provider, A.GET_POOL),
                Call(self.provider, A.GET_PRICE_ORACLE),
                Call(self.provider, A.GET_POOL_DATA_PROVIDER),
                Call(self.provider, A.GET_MARKET_ID),
            ],
            block,
        )
        if not all(r.success for r in res[:2]):
            raise RuntimeError(f"{self.protocol_id}: cannot read PoolAddressesProvider {self.provider}")
        self.pool = to_checksum_address(res[0].value)
        self.oracle = to_checksum_address(res[1].value)
        self.data_provider = to_checksum_address(res[2].value) if res[2].success else ""
        self.market_id = res[3].value if res[3].success else ""
        expected_pool = self.spec.addresses.get("pool")
        if expected_pool and expected_pool.lower() != self.pool.lower():
            log.warning("%s: provider returns pool %s, registry lists %s", self.protocol_id, self.pool, expected_pool)

        res = await mc.run(
            [
                Call(self.pool, A.POOL_REVISION),
                Call(self.pool, A.FLASHLOAN_PREMIUM_TOTAL),
                Call(self.pool, A.GET_LIQUIDATION_LOGIC),
                Call(self.pool, A.GET_RESERVES_LIST),
                Call(self.oracle, A.BASE_CURRENCY),
                Call(self.oracle, A.BASE_CURRENCY_UNIT),
            ],
            block,
        )
        self.pool_revision = res[0].value if res[0].success else 0
        self.flash_premium_bps = int(res[1].value)
        self.liquidation_logic = to_checksum_address(res[2].value)
        reserves_list = [to_checksum_address(a) for a in res[3].value]
        base_currency = res[4].value if res[4].success else None
        self.base_currency_unit = int(res[5].value)
        if base_currency and int(base_currency, 16) != 0:
            raise RuntimeError(
                f"{self.protocol_id}: oracle base currency is {base_currency}, not USD; "
                "USD conversion in this adapter assumes a USD-denominated market"
            )

        res = await mc.run(
            [
                Call(self.liquidation_logic, A.CLOSE_FACTOR_HF_THRESHOLD, allow_failure=False),
                Call(self.liquidation_logic, A.MIN_BASE_MAX_CLOSE_FACTOR_THRESHOLD, allow_failure=False),
                Call(self.liquidation_logic, A.MIN_LEFTOVER_BASE, allow_failure=False),
            ],
            block,
        )
        self.constants = LiquidationConstants(
            close_factor_hf_threshold=int(res[0].value),
            min_base_max_close_factor_threshold=int(res[1].value),
            min_leftover_base=int(res[2].value),
        )

        calls = []
        for asset in reserves_list:
            calls.append(Call(self.pool, A.GET_RESERVE_DATA, (asset,)))
            calls.append(Call(asset, ERC20_SYMBOL))
            calls.append(Call(asset, ERC20_SYMBOL_BYTES32))
        res = await mc.run(calls, block)
        self.reserves.clear()
        self.reserves_by_id.clear()
        for i, asset in enumerate(reserves_list):
            rd, sym, sym32 = res[3 * i], res[3 * i + 1], res[3 * i + 2]
            if not rd.success:
                log.warning("%s: getReserveData failed for %s", self.protocol_id, asset)
                continue
            data = rd.value
            symbol = sym.value if sym.success else (sym32.value.rstrip(b"\0").decode(errors="ignore") if sym32.success else asset[:8])
            reserve = Reserve(
                asset=asset,
                symbol=symbol,
                id=int(data[7]),
                a_token=to_checksum_address(data[8]),
                v_token=to_checksum_address(data[10]),
                config=ReserveConfig.decode(int(data[0])),
            )
            self.reserves[asset] = reserve
            self.reserves_by_id[reserve.id] = reserve
        await self.refresh_market(block)

    async def refresh_market(self, block: int | str) -> None:
        """Per-cycle refresh of prices, configs, virtual balances, grace periods."""
        assets = list(self.reserves)
        calls: list[Call] = [Call(self.oracle, A.GET_ASSETS_PRICES, (assets,))]
        for a in assets:
            calls.append(Call(self.pool, A.GET_CONFIGURATION, (a,)))
            calls.append(Call(self.pool, A.GET_VIRTUAL_UNDERLYING_BALANCE, (a,)))
            calls.append(Call(self.pool, A.GET_LIQUIDATION_GRACE_PERIOD, (a,)))
        res = await self.ctx.multicall.run(calls, block)
        if res[0].success:
            prices = list(res[0].value)
        else:
            # One broken feed reverts the batch getter; fall back per asset
            # (0 = unknown price; such reserves are skipped when quoting).
            single = await self.ctx.multicall.run([Call(self.oracle, A.GET_ASSET_PRICE, (a,)) for a in assets], block)
            prices = [int(r.value) if r.success else 0 for r in single]
        snap = MarketSnapshot(block=block if isinstance(block, int) else 0)
        for i, a in enumerate(assets):
            snap.prices[a] = int(prices[i])
            cfg, vb, grace = res[1 + 3 * i], res[2 + 3 * i], res[3 + 3 * i]
            r = self.reserves[a]
            if cfg.success:
                r.config = ReserveConfig.decode(int(cfg.value))
            r.virtual_balance = int(vb.value) if vb.success else 0
            r.grace_until = int(grace.value) if grace.success else 0
        self.snapshot = snap

    async def _ensure_emodes(self, ids: set[int], block: int | str) -> None:
        missing = sorted(i for i in ids if i and i not in self.emodes)
        if not missing:
            return
        calls = []
        for i in missing:
            calls.append(Call(self.pool, A.GET_EMODE_COLLATERAL_CONFIG, (i,)))
            calls.append(Call(self.pool, A.GET_EMODE_COLLATERAL_BITMAP, (i,)))
        res = await self.ctx.multicall.run(calls, block)
        for n, i in enumerate(missing):
            cfg, bitmap = res[2 * n], res[2 * n + 1]
            if not (cfg.success and bitmap.success):
                continue
            ltv, lt, bonus = cfg.value
            self.emodes[i] = EMode(i, int(ltv), int(lt), int(bonus), int(bitmap.value))

    def describe(self) -> dict[str, Any]:
        assert self.constants is not None
        return {
            "protocol_id": self.protocol_id,
            "market_id": self.market_id,
            "pool_addresses_provider": self.provider,
            "pool": self.pool,
            "oracle": self.oracle,
            "data_provider": self.data_provider,
            "pool_revision": self.pool_revision,
            "flash_premium_bps": self.flash_premium_bps,
            "liquidation_logic": self.liquidation_logic,
            "close_factor_hf_threshold": self.constants.close_factor_hf_threshold,
            "min_base_max_close_factor_threshold": self.constants.min_base_max_close_factor_threshold,
            "min_leftover_base": self.constants.min_leftover_base,
            "default_close_factor_bps": self.constants.default_close_factor_bps,
            "base_currency_unit": self.base_currency_unit,
            "access_policy": self.access_policy.value,
            "reserves": [
                {
                    "symbol": r.symbol,
                    "asset": r.asset,
                    "id": r.id,
                    "decimals": r.decimals,
                    "ltv": r.config.ltv,
                    "liq_threshold": r.config.liquidation_threshold,
                    "liq_bonus": r.config.liquidation_bonus,
                    "protocol_fee": r.config.liquidation_protocol_fee,
                    "active": r.config.active,
                    "paused": r.config.paused,
                    "frozen": r.config.frozen,
                    "flashloan": r.config.flashloan_enabled,
                    "virtual_balance": r.virtual_balance,
                    "price_usd": self.usd_price(r.asset),
                }
                for r in sorted(self.reserves.values(), key=lambda r: r.id)
            ],
        }

    # ------------------------------------------------------------ discovery

    def discovery_events(self) -> Sequence[EventSpec]:
        # Borrow(reserve indexed, user, onBehalfOf indexed, amount, interestRateMode, borrowRate, referralCode indexed)
        # onBehalfOf (topic2) is the account that owns the debt. Every path
        # that creates debt (borrow, credit delegation, flash loan left open
        # as debt) emits Borrow.
        return [
            EventSpec(
                kind="borrow",
                address=self.spec.addresses["pool"] if not self.pool else self.pool,
                topic0=A.BORROW_TOPIC,
                account_of=lambda lg: topic_to_address(lg["topics"][2]) if len(lg["topics"]) > 2 else None,
            )
        ]

    # ---------------------------------------------------------------- state

    def _usd(self, amount: int, decimals: int, price: int) -> float:
        return amount * price / (10**decimals) / self.base_currency_unit

    async def get_position_states(self, accounts: Sequence[str], block: int) -> list[PositionState]:
        if not accounts:
            return []
        calls: list[Call] = []
        for acct in accounts:
            calls.append(Call(self.pool, A.GET_USER_ACCOUNT_DATA, (acct,)))
            calls.append(Call(self.pool, A.GET_USER_CONFIGURATION, (acct,)))
            calls.append(Call(self.pool, A.GET_USER_EMODE, (acct,)))
        res = await self.ctx.multicall.run(calls, block)

        states: list[PositionState] = []
        detail_needed: list[int] = []
        watch = self.ctx.settings.watch_health_factor
        for i, acct in enumerate(accounts):
            uad, ucfg, uem = res[3 * i], res[3 * i + 1], res[3 * i + 2]
            if not uad.success:
                states.append(
                    PositionState(self.protocol_id, self.ctx.chain.key, acct, block, None, None, 0.0, 0.0,
                                  extra={"error": "getUserAccountData failed"})
                )
                continue
            tot_col, tot_debt, _avail, cur_lt, ltv, hf = (int(x) for x in uad.value)
            has_debt = tot_debt > 0
            st = PositionState(
                protocol_id=self.protocol_id,
                chain=self.ctx.chain.key,
                account=acct,
                block_number=block,
                health_factor=(hf / 1e18) if has_debt else None,
                health_factor_raw=hf if has_debt else None,
                total_collateral_usd=tot_col / self.base_currency_unit,
                total_debt_usd=tot_debt / self.base_currency_unit,
                extra={
                    "total_collateral_base": tot_col,
                    "total_debt_base": tot_debt,
                    "current_liquidation_threshold": cur_lt,
                    "ltv": ltv,
                    "user_config": int(ucfg.value) if ucfg.success else 0,
                    "emode": int(uem.value) if uem.success else 0,
                    "detailed": False,
                },
            )
            states.append(st)
            if has_debt and hf / 1e18 < watch:
                detail_needed.append(i)

        await self._ensure_emodes({states[i].extra["emode"] for i in detail_needed}, block)
        # Per-reserve balances only for accounts near or below the threshold.
        calls = []
        index: list[tuple[int, Reserve, str]] = []
        for i in detail_needed:
            st = states[i]
            ucfg = st.extra["user_config"]
            for r in self.reserves.values():
                if user_is_using_as_collateral(ucfg, r.id):
                    calls.append(Call(r.a_token, ERC20_BALANCE_OF, (st.account,)))
                    index.append((i, r, "collateral"))
                if user_is_borrowing(ucfg, r.id):
                    calls.append(Call(r.v_token, ERC20_BALANCE_OF, (st.account,)))
                    index.append((i, r, "debt"))
        if calls:
            res = await self.ctx.multicall.run(calls, block)
            for (i, r, kind), out in zip(index, res):
                if not out.success or int(out.value) == 0:
                    continue
                amount = int(out.value)
                price = self.snapshot.prices.get(r.asset, 0)
                pos = AssetPosition(
                    asset=r.asset,
                    symbol=r.symbol,
                    decimals=r.decimals,
                    amount=amount,
                    price=price,
                    value_usd=self._usd(amount, r.decimals, price),
                    enabled_as_collateral=kind == "collateral",
                )
                (states[i].collaterals if kind == "collateral" else states[i].debts).append(pos)
            for i in detail_needed:
                states[i].extra["detailed"] = True
        return states

    def is_liquidatable(self, state: PositionState, block_timestamp: int, liquidator: str | None = None) -> Eligibility:
        if state.health_factor_raw is None:
            return Eligibility(False, ["no debt"])
        if state.health_factor_raw >= HEALTH_FACTOR_LIQUIDATION_THRESHOLD:
            return Eligibility(False, [f"health factor {state.health_factor:.4f} >= 1"])
        el = Eligibility(False, [f"health factor {state.health_factor:.4f} < 1 (Pool.getUserAccountData)"])
        if liquidator and liquidator.lower() == state.account.lower():
            el.blockers.append("SelfLiquidation: liquidator == borrower")
            return el
        if not state.extra.get("detailed"):
            el.blockers.append("per-reserve balances not loaded")
            return el
        valid_col = [c for c in state.collaterals if self._reserve_ok(c.asset, block_timestamp, el, "collateral")]
        valid_debt = [d for d in state.debts if self._reserve_ok(d.asset, block_timestamp, el, "debt")]
        if not valid_col:
            el.blockers.append("no liquidatable collateral (enabled, active, unpaused, past grace period)")
        if not valid_debt:
            el.blockers.append("no liquidatable debt reserve")
        el.liquidatable = bool(valid_col and valid_debt)
        return el

    def _reserve_ok(self, asset: str, ts: int, el: Eligibility, role: str) -> bool:
        r = self.reserves.get(asset)
        if r is None:
            el.blockers.append(f"{role} {asset} not a listed reserve")
            return False
        if not r.config.active:
            el.blockers.append(f"{role} {r.symbol}: ReserveInactive")
            return False
        if r.config.paused:
            el.blockers.append(f"{role} {r.symbol}: ReservePaused")
            return False
        if r.grace_until >= ts:
            el.blockers.append(f"{role} {r.symbol}: liquidation grace period until {r.grace_until}")
            return False
        return True

    def liquidation_bonus_for(self, emode_id: int, collateral: Reserve) -> int:
        if emode_id:
            em = self.emodes.get(emode_id)
            if em and emode_bitmap_has(em.collateral_bitmap, collateral.id):
                return em.liquidation_bonus
        return collateral.config.liquidation_bonus

    async def get_liquidation_quotes(self, state: PositionState, max_pairs: int) -> list[LiquidationQuote]:
        assert self.constants is not None
        quotes: list[LiquidationQuote] = []
        emode = int(state.extra.get("emode", 0))
        for col in state.collaterals:
            cr = self.reserves[col.asset]
            bonus = self.liquidation_bonus_for(emode, cr)
            for debt in state.debts:
                dr = self.reserves[debt.asset]
                col_price = self.snapshot.prices[col.asset]
                debt_price = self.snapshot.prices[debt.asset]
                if col_price == 0 or debt_price == 0:
                    continue
                inp = LiquidationInputs(
                    debt_to_cover=MAX_UINT256,
                    borrower_collateral_balance=col.amount,
                    borrower_reserve_debt=debt.amount,
                    collateral_price=col_price,
                    debt_price=debt_price,
                    collateral_decimals=cr.decimals,
                    debt_decimals=dr.decimals,
                    liquidation_bonus_bps=bonus,
                    liquidation_protocol_fee_bps=cr.config.liquidation_protocol_fee,
                    health_factor=int(state.health_factor_raw or 0),
                    total_debt_base=int(state.extra["total_debt_base"]),
                )
                planned = plan_debt_to_cover(inp, self.constants)
                if planned is None:
                    continue
                dtc, result = planned
                notes = []
                if result.close_factor_capped:
                    notes.append("close factor 50% applies")
                if dtc != MAX_UINT256:
                    notes.append("debtToCover reduced to satisfy MustNotLeaveDust")
                # receiveAToken=false withdraws underlying: the collateral
                # reserve must hold enough virtual balance. (For same-asset
                # pairs the repaid debt re-enters the reserve first, but a
                # same-reserve flash loan takes out about as much.)
                if result.collateral_to_liquidator > cr.virtual_balance:
                    notes.append("insufficient collateral reserve liquidity for underlying withdrawal")
                    continue
                q = LiquidationQuote(
                    protocol_id=self.protocol_id,
                    chain=self.ctx.chain.key,
                    account=state.account,
                    block_number=state.block_number,
                    liquidation_type=self.liquidation_type,
                    collateral_asset=cr.asset,
                    collateral_symbol=cr.symbol,
                    collateral_decimals=cr.decimals,
                    debt_asset=dr.asset,
                    debt_symbol=dr.symbol,
                    debt_decimals=dr.decimals,
                    debt_to_cover_param=dtc,
                    expected_debt_repaid=result.actual_debt_to_liquidate,
                    expected_collateral_out=result.collateral_to_liquidator,
                    protocol_fee_collateral=result.protocol_fee,
                    liquidation_bonus_bps=bonus,
                    close_factor_capped=result.close_factor_capped,
                    full_liquidation=result.liquidates_all_debt,
                    debt_value_usd=self._usd(result.actual_debt_to_liquidate, dr.decimals, debt_price),
                    collateral_value_usd=self._usd(result.collateral_to_liquidator, cr.decimals, col_price),
                    notes=notes,
                )
                q.auction_discount = (
                    1 - q.debt_value_usd / q.collateral_value_usd if q.collateral_value_usd > 0 else None
                )
                quotes.append(q)
        quotes.sort(key=lambda q: q.gross_bonus_usd, reverse=True)
        return quotes[:max_pairs]

    # ------------------------------------------------------------ execution

    def flash_sources(self) -> list[str]:
        return ["aave_v3"]

    def flash_amount_for(self, quote: LiquidationQuote) -> int:
        buf = self.ctx.settings.flash_amount_buffer_bps
        return quote.expected_debt_repaid * (10_000 + buf) // 10_000 + 1

    async def plan_funding(self, quote: LiquidationQuote, wallet_balance: int | None) -> FundingPlan | None:
        return choose_funding(
            quote.debt_asset, self.flash_amount_for(quote), wallet_balance, [AaveV3FlashSource(self)]
        )

    def build_liquidation_transaction(
        self, quote: LiquidationQuote, swap: SwapQuote | None, funding: FundingPlan, min_profit: int
    ) -> ExecutionPlan:
        same = quote.collateral_asset == quote.debt_asset
        slippage = self.ctx.settings.slippage_bps
        min_out = 0 if swap is None else swap.amount_out * (10_000 - slippage) // 10_000
        params = {
            "pool": self.pool,
            "collateralAsset": quote.collateral_asset,
            "debtAsset": quote.debt_asset,
            "borrower": quote.account,
            "debtToCover": str(quote.debt_to_cover_param),
            "swapKind": 0 if same or swap is None else SWAP_KIND[swap.kind],
            "router": swap.router if swap and not same else "0x0000000000000000000000000000000000000000",
            "path": swap.encoded_path if swap and not same else "0x",
            "minAmountOut": str(min_out),
            "minProfit": str(max(0, min_profit)),
            "fundingMode": funding.mode,
            "flashAmount": str(funding.amount),
        }
        return ExecutionPlan(
            protocol_id=self.protocol_id,
            chain=self.ctx.chain.key,
            quote=quote,
            swap=swap,
            funding=funding,
            executor_kind="AaveV3LiquidationExecutor",
            params=params,
        )

    async def simulate_liquidation(self, plan: ExecutionPlan, simulator: Any) -> SimulationResult:
        from ...simulation.aave_v3 import simulate_aave_v3_liquidation

        try:
            return await simulate_aave_v3_liquidation(self, plan, simulator)
        except Exception as exc:  # noqa: BLE001 - infra failure must not look like a revert
            log.exception("simulation infrastructure error")
            return SimulationResult(status=SimStatus.ERROR, detail=f"{type(exc).__name__}: {exc}")

    # -------------------------------------------------------------- pricing

    def usd_price(self, asset: str) -> float | None:
        p = self.snapshot.prices.get(to_checksum_address(asset))
        return p / self.base_currency_unit if p else None

    def native_usd_price(self) -> float | None:
        return self.usd_price(self.ctx.chain.wrapped_native)
