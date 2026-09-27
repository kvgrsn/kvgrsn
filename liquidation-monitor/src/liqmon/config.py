"""Configuration loading: YAML files in ./config plus LIQMON_* env overrides.

Secrets (private keys, bot tokens) are never read from YAML; only from env.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from eth_utils import to_checksum_address

DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


def _config_dir() -> Path:
    return Path(os.environ.get("LIQMON_CONFIG_DIR", DEFAULT_CONFIG_DIR))


def _load_yaml(name: str) -> Any:
    with open(_config_dir() / name, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _env_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class ChainConfig:
    key: str
    chain_id: int
    name: str
    native_symbol: str
    wrapped_native: str
    multicall3: str
    rpc_urls: tuple[str, ...]
    confirmations: int
    reorg_depth: int
    max_log_range: int
    requests_per_second: float
    multicall_chunk: int
    rpc_batch_size: int


@dataclass(frozen=True)
class ProtocolSpec:
    id: str
    chain: str
    protocol: str
    adapter: str
    enabled: bool
    liquidation_type: str
    access: str
    addresses: dict[str, str]
    deployment_block: int | None
    abi_source: str
    flash_liquidity: tuple[str, ...]
    verification: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class VenueConfig:
    name: str
    kind: str  # uniswap_v3 | uniswap_v2
    factory: str
    router: str
    quoter: str | None = None
    fee_tiers: tuple[int, ...] = ()
    fee_bps: int = 0


@dataclass(frozen=True)
class DexConfig:
    chain: str
    connectors: dict[str, str]
    venues: tuple[VenueConfig, ...]


@dataclass(frozen=True)
class Settings:
    dry_run: bool
    db_path: str
    min_profit_usd: float
    slippage_bps: int
    safety_buffer_usd: float
    safety_buffer_bps: int
    gas_price_multiplier: float
    flash_amount_buffer_bps: int
    watch_health_factor: float
    max_pairs_per_position: int
    max_simulations_per_cycle: int
    anvil_bin: str
    anvil_port: int
    simulation_timeout_s: int
    alert_min_profit_usd: float
    alert_desktop: bool
    alert_cooldown_s: int
    scan_interval_s: int


def load_chains() -> dict[str, ChainConfig]:
    raw = _load_yaml("chains.yaml")
    chains: dict[str, ChainConfig] = {}
    for key, c in raw.items():
        env_rpc = os.environ.get(f"LIQMON_RPC_{key.upper()}")
        urls = [u.strip() for u in env_rpc.split(",") if u.strip()] if env_rpc else c["rpc_urls"]
        chains[key] = ChainConfig(
            key=key,
            chain_id=int(c["chain_id"]),
            name=c["name"],
            native_symbol=c["native_symbol"],
            wrapped_native=to_checksum_address(c["wrapped_native"]),
            multicall3=to_checksum_address(c["multicall3"]),
            rpc_urls=tuple(urls),
            confirmations=int(c["confirmations"]),
            reorg_depth=int(c["reorg_depth"]),
            max_log_range=int(c["max_log_range"]),
            requests_per_second=float(c["requests_per_second"]),
            multicall_chunk=int(c["multicall_chunk"]),
            rpc_batch_size=int(c["rpc_batch_size"]),
        )
    return chains


def load_protocols() -> list[ProtocolSpec]:
    raw = _load_yaml("protocols.yaml")
    specs = []
    for p in raw:
        specs.append(
            ProtocolSpec(
                id=p["id"],
                chain=p["chain"],
                protocol=p["protocol"],
                adapter=p["adapter"],
                enabled=bool(p.get("enabled", False)),
                liquidation_type=p["liquidation_type"],
                access=p["access"],
                addresses={k: to_checksum_address(v) for k, v in p["addresses"].items()},
                deployment_block=p.get("deployment_block"),
                abi_source=p.get("abi_source", ""),
                flash_liquidity=tuple(p.get("flash_liquidity", [])),
                verification=p.get("verification", {}) or {},
            )
        )
    return specs


def load_dex() -> dict[str, DexConfig]:
    raw = _load_yaml("dex.yaml")
    out: dict[str, DexConfig] = {}
    for chain, d in raw.items():
        venues = tuple(
            VenueConfig(
                name=v["name"],
                kind=v["kind"],
                factory=to_checksum_address(v["factory"]),
                router=to_checksum_address(v["router"]),
                quoter=to_checksum_address(v["quoter"]) if v.get("quoter") else None,
                fee_tiers=tuple(v.get("fee_tiers", [])),
                fee_bps=int(v.get("fee_bps", 0)),
            )
            for v in d["venues"]
        )
        out[chain] = DexConfig(
            chain=chain,
            connectors={k: to_checksum_address(v) for k, v in d["connectors"].items()},
            venues=venues,
        )
    return out


def load_settings() -> Settings:
    raw: dict[str, Any] = _load_yaml("settings.yaml")
    values: dict[str, Any] = {}
    for f in Settings.__dataclass_fields__.values():
        default = raw[f.name]
        env = os.environ.get(f"LIQMON_{f.name.upper()}")
        if env is None:
            values[f.name] = default
        elif isinstance(default, bool):
            values[f.name] = _env_bool(env)
        elif isinstance(default, int):
            values[f.name] = int(env)
        elif isinstance(default, float):
            values[f.name] = float(env)
        else:
            values[f.name] = env
    return Settings(**values)
