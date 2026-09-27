"""Adapter registry: maps the `adapter` key in config/protocols.yaml to a class."""

from __future__ import annotations

from ..config import ProtocolSpec
from .aave_v3.adapter import AaveV3Adapter
from .base import AdapterContext, ProtocolAdapter

ADAPTERS: dict[str, type[ProtocolAdapter]] = {
    "aave_v3": AaveV3Adapter,
}


def build_adapter(ctx: AdapterContext, spec: ProtocolSpec) -> ProtocolAdapter:
    try:
        cls = ADAPTERS[spec.adapter]
    except KeyError as exc:
        raise ValueError(f"no adapter registered for {spec.adapter!r} ({spec.id})") from exc
    return cls(ctx, spec)
