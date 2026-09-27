"""Minimal ABI helpers built on eth_abi (no web3.py dependency).

``Fn("balanceOf(address)", ["uint256"])`` gives an encoder/decoder pair.
Revert data is decoded for Error(string), Panic(uint256) and any custom
errors registered via ``register_errors``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from eth_utils import keccak, to_checksum_address

MAX_UINT256 = 2**256 - 1


def selector(signature: str) -> bytes:
    return keccak(text=signature)[:4]


def topic(signature: str) -> str:
    return "0x" + keccak(text=signature).hex()


def _split_types(sig_args: str) -> list[str]:
    """Split a top-level comma-separated type list, respecting tuples."""
    out, depth, cur = [], 0, ""
    for ch in sig_args:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur:
        out.append(cur)
    return out


@dataclass(frozen=True)
class Fn:
    signature: str
    outputs: Sequence[str] = field(default_factory=tuple)

    @property
    def selector(self) -> bytes:
        return selector(self.signature)

    @property
    def inputs(self) -> list[str]:
        args = self.signature[self.signature.index("(") + 1 : self.signature.rindex(")")]
        return _split_types(args)

    def encode(self, *args: Any) -> str:
        return "0x" + (self.selector + abi_encode(self.inputs, list(args))).hex()

    def encode_bytes(self, *args: Any) -> bytes:
        return self.selector + abi_encode(self.inputs, list(args))

    def decode(self, data: str | bytes) -> tuple[Any, ...]:
        raw = hex_to_bytes(data)
        return tuple(abi_decode(list(self.outputs), raw))

    def decode_one(self, data: str | bytes) -> Any:
        return self.decode(data)[0]


def hex_to_bytes(data: str | bytes) -> bytes:
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    if data.startswith("0x"):
        data = data[2:]
    return bytes.fromhex(data)


def checksum(addr: str) -> str:
    return to_checksum_address(addr)


def topic_to_address(t: str) -> str:
    return to_checksum_address("0x" + t[-40:])


def pad_address_topic(addr: str) -> str:
    return "0x" + "0" * 24 + addr.lower().removeprefix("0x")


# ----------------------------------------------------------------- reverts

_ERROR_STRING = selector("Error(string)")
_PANIC = selector("Panic(uint256)")
_CUSTOM_ERRORS: dict[bytes, tuple[str, list[str]]] = {}


def register_errors(signatures: Iterable[str]) -> None:
    for sig in signatures:
        name = sig[: sig.index("(")]
        args = _split_types(sig[sig.index("(") + 1 : sig.rindex(")")])
        _CUSTOM_ERRORS[selector(sig)] = (name, args)


def decode_revert(data: str | bytes | None) -> str:
    if not data:
        return "reverted without data"
    raw = hex_to_bytes(data)
    if len(raw) < 4:
        return f"revert data 0x{raw.hex()}"
    sel, body = raw[:4], raw[4:]
    try:
        if sel == _ERROR_STRING:
            return abi_decode(["string"], body)[0]
        if sel == _PANIC:
            return f"Panic(0x{abi_decode(['uint256'], body)[0]:x})"
        if sel in _CUSTOM_ERRORS:
            name, types = _CUSTOM_ERRORS[sel]
            args = abi_decode(types, body) if types else ()
            return f"{name}({', '.join(str(a) for a in args)})"
    except Exception:  # noqa: BLE001 - best effort decoding
        pass
    return f"unknown error 0x{raw.hex()[:200]}"


# Common ERC20 functions
ERC20_BALANCE_OF = Fn("balanceOf(address)", ["uint256"])
ERC20_DECIMALS = Fn("decimals()", ["uint8"])
ERC20_SYMBOL = Fn("symbol()", ["string"])
ERC20_SYMBOL_BYTES32 = Fn("symbol()", ["bytes32"])
ERC20_APPROVE = Fn("approve(address,uint256)", ["bool"])
ERC20_TRANSFER = Fn("transfer(address,uint256)", ["bool"])
