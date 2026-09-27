"""DEX route discovery using executable quotes.

* Uniswap-V3-style venues (Uniswap V3, PancakeSwap V3): QuoterV2.quoteExactInput
  runs the real swap against current pool state inside eth_call, so the
  quote includes fees, tick crossings and price impact.
* Uniswap-V2-style venues (PancakeSwap V2, Trader Joe V1, Pangolin):
  Router.getAmountsOut, which applies the constant-product formula with the
  venue fee on live reserves.

Candidate paths: direct pools on every venue/fee tier, plus 2-hop routes via
configured connector tokens. Pool existence is checked first (one multicall
over the factories) so quotes are only requested for real pools. All quotes
for one swap are pinned to the same block.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from itertools import product
from typing import Callable

from eth_abi import encode as abi_encode
from eth_utils import to_checksum_address

from ..config import DexConfig, VenueConfig
from ..models import SwapQuote
from ..rpc.abi import Fn
from ..rpc.client import RpcClient
from ..rpc.multicall import Call, Multicall

log = logging.getLogger(__name__)

ZERO = "0x0000000000000000000000000000000000000000"

V3_GET_POOL = Fn("getPool(address,address,uint24)", ["address"])
V3_QUOTE_EXACT_INPUT = Fn("quoteExactInput(bytes,uint256)", ["uint256", "uint160[]", "uint32[]", "uint256"])
V2_GET_PAIR = Fn("getPair(address,address)", ["address"])
V2_GET_AMOUNTS_OUT = Fn("getAmountsOut(uint256,address[])", ["uint256[]"])

V2_SWAP_GAS = 110_000
V2_EXTRA_HOP_GAS = 60_000


def encode_v3_path(tokens: list[str], fees: list[int]) -> bytes:
    if len(tokens) != len(fees) + 1:
        raise ValueError("tokens/fees length mismatch")
    out = bytes.fromhex(tokens[0][2:])
    for fee, tok in zip(fees, tokens[1:]):
        out += fee.to_bytes(3, "big") + bytes.fromhex(tok[2:])
    return out


def encode_v2_path(tokens: list[str]) -> bytes:
    return abi_encode(["address[]"], [tokens])


def restrict_dex(dex: DexConfig, venues: list[str] | None, connectors: list[str] | None) -> DexConfig:
    """Limit routing to some venues / connector tokens (by name).

    Useful for backtests on slow archive forks, where every untouched pool
    costs many remote storage reads. An empty connector list means direct
    pools only.
    """
    import dataclasses

    v = tuple(x for x in dex.venues if not venues or x.name in venues)
    if venues and not v:
        raise ValueError(f"no venue named {venues}; known: {[x.name for x in dex.venues]}")
    c = dex.connectors if connectors is None else {k: a for k, a in dex.connectors.items() if k in connectors}
    return dataclasses.replace(dex, venues=v, connectors=c)


@dataclass(frozen=True)
class _Candidate:
    venue: VenueConfig
    tokens: tuple[str, ...]
    fees: tuple[int, ...]


class RouteFinder:
    def __init__(self, client: RpcClient, multicall_address: str, dex: DexConfig, quote_chunk: int = 20):
        self.client = client
        self.dex = dex
        # Quoter calls are gas-heavy; keep multicall chunks small so they fit
        # under node eth_call gas caps.
        self.quote_mc = Multicall(client, multicall_address, chunk_size=quote_chunk)
        self.lookup_mc = Multicall(client, multicall_address, chunk_size=300)
        self._pool_cache: dict[tuple[str, str, str, int], bool] = {}

    def _pool_key(self, venue: VenueConfig, a: str, b: str, fee: int) -> tuple[str, str, str, int]:
        x, y = sorted((a.lower(), b.lower()))
        return (venue.name, x, y, fee)

    async def _existing_pools(self, pairs: set[tuple[str, str]], block: int | str) -> None:
        calls: list[Call] = []
        keys: list[tuple[str, str, str, int]] = []
        for venue in self.dex.venues:
            for a, b in pairs:
                tiers = venue.fee_tiers if venue.kind == "uniswap_v3" else (venue.fee_bps,)
                for fee in tiers:
                    key = self._pool_key(venue, a, b, fee)
                    if key in self._pool_cache or key in keys:
                        continue
                    if venue.kind == "uniswap_v3":
                        calls.append(Call(venue.factory, V3_GET_POOL, (a, b, fee)))
                    else:
                        calls.append(Call(venue.factory, V2_GET_PAIR, (a, b)))
                    keys.append(key)
        if not calls:
            return
        res = await self.lookup_mc.run(calls, block)
        for key, r in zip(keys, res):
            self._pool_cache[key] = bool(r.success and r.value and int(r.value, 16) != 0)

    def _exists(self, venue: VenueConfig, a: str, b: str, fee: int) -> bool:
        return self._pool_cache.get(self._pool_key(venue, a, b, fee), False)

    def _candidates(self, token_in: str, token_out: str) -> list[_Candidate]:
        cands: list[_Candidate] = []
        connectors = [c for c in self.dex.connectors.values() if c.lower() not in (token_in.lower(), token_out.lower())]
        for venue in self.dex.venues:
            tiers = venue.fee_tiers if venue.kind == "uniswap_v3" else (venue.fee_bps,)
            for fee in tiers:
                if self._exists(venue, token_in, token_out, fee):
                    cands.append(_Candidate(venue, (token_in, token_out), (fee,)))
            for c in connectors:
                for f1, f2 in product(tiers, tiers):
                    if self._exists(venue, token_in, c, f1) and self._exists(venue, c, token_out, f2):
                        cands.append(_Candidate(venue, (token_in, c, token_out), (f1, f2)))
        return cands

    def _quote_call(self, cand: _Candidate, amount_in: int) -> Call:
        if cand.venue.kind == "uniswap_v3":
            assert cand.venue.quoter is not None
            path = encode_v3_path(list(cand.tokens), list(cand.fees))
            return Call(cand.venue.quoter, V3_QUOTE_EXACT_INPUT, (path, amount_in))
        return Call(cand.venue.router, V2_GET_AMOUNTS_OUT, (amount_in, list(cand.tokens)))

    def _to_quote(self, cand: _Candidate, amount_in: int, value: object) -> SwapQuote | None:
        if cand.venue.kind == "uniswap_v3":
            amount_out, _sqrt, _ticks, gas_est = value  # type: ignore[misc]
            encoded = "0x" + encode_v3_path(list(cand.tokens), list(cand.fees)).hex()
        else:
            amounts = value
            amount_out = amounts[-1]  # type: ignore[index]
            gas_est = V2_SWAP_GAS + V2_EXTRA_HOP_GAS * (len(cand.tokens) - 2)
            encoded = "0x" + encode_v2_path(list(cand.tokens)).hex()
        if int(amount_out) == 0:
            return None
        return SwapQuote(
            venue=cand.venue.name,
            kind=cand.venue.kind,
            router=cand.venue.router,
            tokens=list(cand.tokens),
            fees=list(cand.fees),
            amount_in=amount_in,
            amount_out=int(amount_out),
            gas_estimate=int(gas_est),
            price_impact_bps=None,
            encoded_path=encoded,
        )

    async def quote_all(self, token_in: str, token_out: str, amount_in: int, block: int | str) -> list[SwapQuote]:
        token_in, token_out = to_checksum_address(token_in), to_checksum_address(token_out)
        tokens = {token_in, token_out, *self.dex.connectors.values()}
        pairs = {(a, b) for a in tokens for b in tokens if a < b}
        await self._existing_pools(pairs, block)
        cands = self._candidates(token_in, token_out)
        if not cands:
            return []
        res = await self.quote_mc.run([self._quote_call(c, amount_in) for c in cands], block, unwrap_single=False)
        quotes = []
        for cand, r in zip(cands, res):
            if not r.success:
                continue
            value = r.value if cand.venue.kind == "uniswap_v3" else r.value[0]
            q = self._to_quote(cand, amount_in, value)
            if q:
                quotes.append(q)
        return quotes

    async def best_route(
        self,
        token_in: str,
        token_out: str,
        amount_in: int,
        block: int | str,
        *,
        gas_cost_in_out_units: Callable[[int], int] | None = None,
    ) -> tuple[SwapQuote | None, list[SwapQuote]]:
        """Best executable route by output net of estimated swap gas.

        Returns (best, all_quotes_sorted).
        """
        if amount_in <= 0:
            return None, []
        quotes = await self.quote_all(token_in, token_out, amount_in, block)

        def net(q: SwapQuote) -> int:
            return q.amount_out - (gas_cost_in_out_units(q.gas_estimate) if gas_cost_in_out_units else 0)

        quotes.sort(key=lambda q: (net(q), -q.hops), reverse=True)
        best = quotes[0] if quotes else None
        if best is not None:
            best.price_impact_bps = await self._price_impact(best, block)
        return best, quotes

    async def _price_impact(self, q: SwapQuote, block: int | str) -> float | None:
        """Compare the executable quote against a 1/1000-size probe of the same path."""
        probe_in = max(1, q.amount_in // 1000)
        cand = _Candidate(
            next(v for v in self.dex.venues if v.name == q.venue), tuple(q.tokens), tuple(q.fees)
        )
        res = await self.quote_mc.run([self._quote_call(cand, probe_in)], block, unwrap_single=False)
        if not res or not res[0].success:
            return None
        value = res[0].value if cand.venue.kind == "uniswap_v3" else res[0].value[0]
        probe_out = value[0] if cand.venue.kind == "uniswap_v3" else value[-1]
        if probe_out == 0:
            return None
        linear = probe_out * q.amount_in / probe_in
        return max(0.0, (1 - q.amount_out / linear) * 10_000)
