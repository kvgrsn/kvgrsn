"""Common interface every protocol adapter implements.

The scanner only talks to this interface, so adding a protocol means adding
an adapter plus a registry entry; nothing else changes.

Contract for implementers
-------------------------
* Read every risk parameter from the deployed contracts (``load``). Never
  copy parameters from another deployment or chain.
* ``access_policy`` must describe the *real* on-chain restriction. If a
  liquidation path is keeper-only, report it and return no quotes; do not
  try to work around it.
* ``is_liquidatable`` must mirror the protocol's own condition, preferably
  by using values the protocol computes on-chain (e.g. Aave's
  getUserAccountData health factor) rather than off-chain approximations.
* Quotes are plans. Only ``simulate_liquidation`` can mark something
  executable.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Sequence

from ..config import ChainConfig, ProtocolSpec, Settings
from ..indexer.event_indexer import EventSpec
from ..models import (
    AccessPolicy,
    AuctionState,
    Eligibility,
    FundingPlan,
    LiquidationQuote,
    LiquidationType,
    PositionState,
    SimulationResult,
    SwapQuote,
)
from ..rpc.client import RpcClient
from ..rpc.multicall import Multicall

if TYPE_CHECKING:
    from ..simulation.simulator import ForkSimulator


@dataclass
class AdapterContext:
    chain: ChainConfig
    client: RpcClient
    multicall: Multicall
    settings: Settings


@dataclass
class ExecutionPlan:
    """Everything needed to simulate or broadcast one liquidation."""

    protocol_id: str
    chain: str
    quote: LiquidationQuote
    swap: SwapQuote | None
    funding: FundingPlan
    executor_kind: str           # which executor contract understands `calldata_params`
    params: dict[str, Any]       # executor-specific parameters (JSON-serialisable)


class ProtocolAdapter(ABC):
    liquidation_type: LiquidationType
    access_policy: AccessPolicy

    def __init__(self, ctx: AdapterContext, spec: ProtocolSpec):
        self.ctx = ctx
        self.spec = spec

    @property
    def protocol_id(self) -> str:
        return self.spec.id

    # ----------------------------------------------------------- lifecycle

    @abstractmethod
    async def load(self, block: int | str = "latest") -> None:
        """Read deployment configuration (reserves, oracle, constants)."""

    async def refresh_market(self, block: int | str) -> None:
        """Per-cycle refresh of market-wide state (prices, reserve flags...)."""

    @abstractmethod
    def describe(self) -> dict[str, Any]:
        """Human-readable summary of what was read on-chain (for `liqmon verify`)."""

    # ----------------------------------------------------------- discovery

    @abstractmethod
    def discovery_events(self) -> Sequence[EventSpec]:
        """Logs that reveal a position owner (fed to the generic indexer)."""

    async def discover_positions(self, from_block: int, to_block: int) -> list[str]:
        """Optional non-event discovery (registries, enumerable vault NFTs...)."""
        return []

    # --------------------------------------------------------------- state

    @abstractmethod
    async def get_position_states(self, accounts: Sequence[str], block: int) -> list[PositionState]:
        """Batch read of position state pinned to ``block``."""

    async def get_position_state(self, account: str, block: int) -> PositionState:
        return (await self.get_position_states([account], block))[0]

    @abstractmethod
    def is_liquidatable(self, state: PositionState, block_timestamp: int, liquidator: str | None = None) -> Eligibility:
        ...

    @abstractmethod
    async def get_liquidation_quotes(self, state: PositionState, max_pairs: int) -> list[LiquidationQuote]:
        ...

    async def get_auction_state(self, auction_id: str, block: int | str = "latest") -> AuctionState | None:
        """Auction-based adapters override this. Non-auction protocols return None."""
        return None

    async def list_active_auctions(self, block: int | str = "latest") -> list[AuctionState]:
        return []

    # ----------------------------------------------------------- execution

    @abstractmethod
    def flash_sources(self) -> list[str]:
        ...

    @abstractmethod
    async def plan_funding(self, quote: LiquidationQuote, wallet_balance: int | None) -> FundingPlan | None:
        ...

    @abstractmethod
    def build_liquidation_transaction(
        self, quote: LiquidationQuote, swap: SwapQuote | None, funding: FundingPlan, min_profit: int
    ) -> ExecutionPlan:
        ...

    @abstractmethod
    async def simulate_liquidation(self, plan: ExecutionPlan, simulator: "ForkSimulator") -> SimulationResult:
        ...

    # -------------------------------------------------------------- pricing

    @abstractmethod
    def usd_price(self, asset: str) -> float | None:
        """USD price of ``asset`` according to the protocol oracle (cached from last read)."""

    @abstractmethod
    def native_usd_price(self) -> float | None:
        ...
