"""Candidate ranking by objective financial metrics.

Order:
1. simulation SUCCESS before anything else (a position is never treated as
   executable on an off-chain estimate),
2. profit in USD (simulated when available, else estimated),
3. then lower capital required, lower gas, fewer swap hops, lower price impact.
"""

from __future__ import annotations

from ..models import Opportunity, SimStatus


def sort_key(o: Opportunity) -> tuple:
    executable = 1 if o.executable else 0
    profit = o.profit_usd if o.profit_usd is not None else float("-inf")
    capital = -(o.funding.amount if o.funding and o.funding.mode == "wallet" else 0)
    gas = -(o.simulation.gas_used or (o.estimate.gas_units if o.estimate else 10**9))
    hops = -(o.swap.hops if o.swap else 0)
    impact = -(o.swap.price_impact_bps if o.swap and o.swap.price_impact_bps is not None else 0.0)
    return (executable, profit, capital, gas, hops, impact)


def rank(opps: list[Opportunity]) -> list[Opportunity]:
    ranked = sorted(opps, key=sort_key, reverse=True)
    for o in ranked:
        o.score = o.profit_usd if o.profit_usd is not None else float("-inf")
        if o.simulation.status != SimStatus.SUCCESS:
            o.score = float("-inf")
    return ranked
