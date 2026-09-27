"""Protocol-agnostic data model shared by adapters, engines and output."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class LiquidationType(str, Enum):
    REPAY_AND_SEIZE = "repay_and_seize"          # Aave/Compound style direct liquidation
    DUTCH_AUCTION = "dutch_auction"              # price decays over time (Maker Clipper, etc.)
    HEALTH_DUTCH = "health_based_discount"       # discount grows as health falls (Euler v2)
    ENGLISH_AUCTION = "english_auction"
    STABILITY_POOL = "stability_pool"            # Liquity-style
    VAULT_KILL = "vault_kill"                    # leveraged-farming vault liquidation


class AccessPolicy(str, Enum):
    PERMISSIONLESS = "permissionless"
    ROUTED_PERMISSIONLESS = "routed_permissionless"  # must go through a designated public contract
    KEEPER_WHITELIST = "keeper_whitelist"            # only authorized keepers; never bypass
    BORROWER_ALLOWLIST = "borrower_allowlist"        # some borrowers restricted to specific liquidators


class SimStatus(str, Enum):
    SUCCESS = "SUCCESS"
    REVERT = "REVERT"
    ERROR = "ERROR"          # simulator/infra failure: says nothing about the opportunity
    SKIPPED = "SKIPPED"      # not simulated (filtered before simulation)
    PENDING = "PENDING"


@dataclass(frozen=True)
class AssetPosition:
    asset: str
    symbol: str
    decimals: int
    amount: int              # raw token units
    price: int               # protocol oracle price, protocol base units
    value_usd: float
    enabled_as_collateral: bool = False


@dataclass
class PositionState:
    protocol_id: str
    chain: str
    account: str
    block_number: int
    health_factor: float | None       # None = no debt
    health_factor_raw: int | None
    total_collateral_usd: float
    total_debt_usd: float
    collaterals: list[AssetPosition] = field(default_factory=list)
    debts: list[AssetPosition] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def has_debt(self) -> bool:
        return self.total_debt_usd > 0 or bool(self.debts)


@dataclass
class Eligibility:
    liquidatable: bool
    reasons: list[str] = field(default_factory=list)       # why it is (or is not) liquidatable
    blockers: list[str] = field(default_factory=list)      # protocol rules currently preventing it


@dataclass
class LiquidationQuote:
    """What the protocol would do for one (collateral, debt) pair.

    Amounts are raw token units computed with the protocol's own integer math
    at ``block_number``; they are an estimate for the next block until
    confirmed by simulation.
    """

    protocol_id: str
    chain: str
    account: str
    block_number: int
    liquidation_type: LiquidationType
    collateral_asset: str
    collateral_symbol: str
    collateral_decimals: int
    debt_asset: str
    debt_symbol: str
    debt_decimals: int
    debt_to_cover_param: int          # value passed to the liquidation function
    expected_debt_repaid: int
    expected_collateral_out: int      # after protocol fee
    protocol_fee_collateral: int
    liquidation_bonus_bps: int
    close_factor_capped: bool         # partial liquidation forced by close factor
    full_liquidation: bool
    debt_value_usd: float
    collateral_value_usd: float
    auction_id: str | None = None
    auction_discount: float | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def gross_bonus_usd(self) -> float:
        return self.collateral_value_usd - self.debt_value_usd


@dataclass
class AuctionState:
    protocol_id: str
    auction_id: str
    collateral_asset: str
    debt_asset: str
    lot: int                   # collateral remaining
    tab: int                   # debt remaining (protocol units)
    start_time: int
    start_price: int           # "top"
    current_price: int         # executable price at the evaluated timestamp
    oracle_price: int | None
    discount_vs_oracle: float | None
    max_discount: float | None
    needs_restart: bool
    seconds_until_restart: int | None
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class SwapQuote:
    venue: str
    kind: str                  # uniswap_v3 | uniswap_v2
    router: str
    tokens: list[str]
    fees: list[int]            # v3 fee tiers per hop (1e-6 units) or v2 fee bps
    amount_in: int
    amount_out: int
    gas_estimate: int
    price_impact_bps: float | None
    encoded_path: str          # hex; v3 packed path, or abi-encoded address[] for v2

    @property
    def hops(self) -> int:
        return len(self.tokens) - 1


@dataclass
class FundingPlan:
    mode: str                  # "flash" | "wallet"
    source: str                # e.g. "aave_v3"
    asset: str
    amount: int
    fee: int
    available_liquidity: int | None = None


@dataclass
class ProfitEstimate:
    debt_repaid: int
    collateral_received: int
    swap_out: int              # debt-asset units received for the collateral (executable quote)
    flash_fee: int
    slippage_buffer: int       # debt-asset units held back for slippage
    gross_collateral_usd: float
    debt_repaid_usd: float
    swap_fee_usd: float        # informational: already inside the executable quote
    protocol_fee_usd: float    # informational: already deducted by the protocol
    flash_fee_usd: float
    slippage_usd: float
    gas_units: int
    gas_price_wei: int
    gas_cost_native: float
    gas_cost_usd: float
    safety_buffer_usd: float
    expected_profit_usd: float
    expected_profit_native: float
    native_price_usd: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SimulationResult:
    status: SimStatus
    block_number: int | None = None
    gas_used: int | None = None
    revert_reason: str | None = None
    token_deltas: dict[str, int] = field(default_factory=dict)   # executor balance deltas by token
    debt_repaid: int | None = None
    collateral_seized: int | None = None
    profit_debt_units: int | None = None
    profit_usd: float | None = None
    detail: str | None = None


@dataclass
class Opportunity:
    quote: LiquidationQuote
    health_factor: float | None
    swap: SwapQuote | None
    funding: FundingPlan | None
    estimate: ProfitEstimate | None
    simulation: SimulationResult
    rejected_reason: str | None = None
    score: float = 0.0
    opportunity_id: int | None = None

    @property
    def executable(self) -> bool:
        return self.simulation.status == SimStatus.SUCCESS and self.rejected_reason is None

    @property
    def profit_usd(self) -> float | None:
        if self.simulation.status == SimStatus.SUCCESS and self.simulation.profit_usd is not None:
            return self.simulation.profit_usd
        return self.estimate.expected_profit_usd if self.estimate else None
