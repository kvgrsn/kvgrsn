"""Fork simulator: owns one anvil fork per chain and the compiled executors."""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

from eth_utils import keccak, to_checksum_address

from ..config import ChainConfig, Settings
from .anvil import AnvilError, AnvilFork

CONTRACTS_OUT = Path(__file__).resolve().parents[3] / "contracts" / "out"

# Deterministic, key-less simulation identity. It is only ever used on the
# local fork via anvil's auto-impersonation; no private key exists for it.
SIM_OWNER = to_checksum_address(keccak(text="liqmon/simulation-owner")[-20:])


@lru_cache(maxsize=8)
def load_artifact(name: str) -> dict:
    path = Path(os.environ.get("LIQMON_CONTRACTS_OUT", CONTRACTS_OUT)) / f"{name}.sol" / f"{name}.json"
    if not path.exists():
        raise AnvilError(f"missing compiled contract {path}. Run: cd contracts && forge build")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def creation_bytecode(name: str) -> str:
    obj = load_artifact(name)["bytecode"]["object"]
    return obj if obj.startswith("0x") else "0x" + obj


class ForkSimulator:
    def __init__(self, chain: ChainConfig, settings: Settings, port: int, fork_url: str | None = None):
        self.chain = chain
        self.settings = settings
        url = fork_url or os.environ.get(f"LIQMON_FORK_RPC_{chain.key.upper()}") or chain.rpc_urls[0]
        self.fork = AnvilFork(settings.anvil_bin, url, port, startup_timeout_s=settings.simulation_timeout_s)
        self.owner = SIM_OWNER
        # Mainnet pricing context for converting simulated gas into USD.
        self.gas_price_wei = 0
        self.native_usd = 0.0

    async def prepare(self, block: int, gas_price_wei: int, native_usd: float) -> None:
        await self.fork.start(block)
        await self.fork.set_balance(self.owner, 10**24)
        self.gas_price_wei = gas_price_wei
        self.native_usd = native_usd

    async def close(self) -> None:
        await self.fork.stop()

    async def deploy(self, name: str, ctor_args: bytes) -> str:
        return await self.fork.deploy(self.owner, creation_bytecode(name), ctor_args)
