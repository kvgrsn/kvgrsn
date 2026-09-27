"""Flash-liquidity route engine.

Decides between wallet capital and flash liquidity for one liquidation.
A flash source is only offered when the executor contract can use it inside
the same transaction (atomicity), so every source declares which executor
entry point consumes it.

Implemented: Aave V3 ``flashLoanSimple`` (same Pool as the liquidation; the
v3.7 Pool has no reentrancy lock between flashLoanSimple and
liquidationCall, and the simulation proves the path end to end).

Catalogued but not wired into an executor yet (see docs/PROTOCOL_RESEARCH.md):
Balancer V2 Vault flashLoan, Uniswap V3 / PancakeSwap V3 pool ``flash``,
DODO V2 pool flashLoan, Morpho-style free flash loans (Lista Moolah).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Sequence

from ..models import FundingPlan


@dataclass(frozen=True)
class FlashQuote:
    source: str
    asset: str
    amount: int
    fee: int
    available: int


class FlashSource(ABC):
    name: str

    @abstractmethod
    def quote(self, asset: str, amount: int) -> FlashQuote | None:
        """Return a quote if ``amount`` of ``asset`` can be flash-borrowed now."""


def choose_funding(
    asset: str,
    amount: int,
    wallet_balance: int | None,
    sources: Sequence[FlashSource],
) -> FundingPlan | None:
    """Prefer wallet capital when it covers the need (no fee); otherwise the
    cheapest flash source with enough liquidity. None if neither works."""
    if wallet_balance is not None and wallet_balance >= amount:
        return FundingPlan(mode="wallet", source="wallet", asset=asset, amount=amount, fee=0)
    quotes = [q for s in sources if (q := s.quote(asset, amount)) is not None]
    if not quotes:
        return None
    best = min(quotes, key=lambda q: q.fee)
    return FundingPlan(
        mode="flash",
        source=best.source,
        asset=asset,
        amount=amount,
        fee=best.fee,
        available_liquidity=best.available,
    )
