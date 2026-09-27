"""Anvil fork management and low-level fork helpers.

One ``AnvilFork`` process is started per chain and re-pointed (anvil_reset)
to the scan block each cycle. Each simulation runs between evm_snapshot and
evm_revert so simulations never contaminate each other.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import subprocess
from typing import Any

from eth_utils import keccak, to_checksum_address

from ..rpc.abi import ERC20_BALANCE_OF, hex_to_bytes
from ..rpc.client import RpcClient, RpcError

log = logging.getLogger(__name__)

# ERC-7201 namespaced storage of OpenZeppelin v5 ERC20Upgradeable (_balances at offset 0).
_OZ5_ERC20_BALANCES_SLOT = int("52c63247e1f47db19d5ce0460030c497f067ca4cebf71ba98eeadabe20bace00", 16)


class AnvilError(RuntimeError):
    pass


def resolve_anvil_bin(configured: str) -> str:
    candidates = [configured, os.path.expanduser("~/.foundry/bin/anvil")]
    for c in candidates:
        path = shutil.which(os.path.expanduser(c)) if c else None
        if path:
            return path
        if c and os.path.isfile(os.path.expanduser(c)) and os.access(os.path.expanduser(c), os.X_OK):
            return os.path.expanduser(c)
    raise AnvilError(
        f"anvil not found (configured: {configured!r}). Install Foundry (https://getfoundry.sh) "
        "or set LIQMON_ANVIL_BIN."
    )


class AnvilFork:
    def __init__(
        self,
        anvil_bin: str,
        fork_url: str,
        port: int,
        *,
        startup_timeout_s: float = 30.0,
        request_timeout_s: float = 300.0,
    ):
        self.anvil_bin = anvil_bin  # resolved lazily: only simulation needs Foundry
        self.fork_url = fork_url
        self.port = port
        self.startup_timeout_s = startup_timeout_s
        # First touches of fork state are fetched lazily from the remote RPC
        # (slow on archive endpoints), so individual requests can take a while.
        self.request_timeout_s = request_timeout_s
        self.proc: subprocess.Popen[bytes] | None = None
        self.client = self._new_client()
        self.fork_block: int | None = None

    async def start(self, fork_block: int) -> None:
        if self.proc and self.proc.poll() is None:
            await self.reset(fork_block)
            return
        args = [
            resolve_anvil_bin(self.anvil_bin),
            "--fork-url",
            self.fork_url,
            "--fork-block-number",
            str(fork_block),
            "--port",
            str(self.port),
            "--host",
            "127.0.0.1",
            "--silent",
            "--no-rate-limit",
            "--auto-impersonate",
        ]
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True)
        deadline = asyncio.get_running_loop().time() + self.startup_timeout_s
        while True:
            if self.proc.poll() is not None:
                err = (self.proc.stderr.read() if self.proc.stderr else b"").decode(errors="ignore")
                raise AnvilError(f"anvil exited during startup: {err[-500:]}")
            try:
                await self.client.chain_id()
                break
            except Exception:  # noqa: BLE001 - not ready yet
                if asyncio.get_running_loop().time() > deadline:
                    await self.stop()
                    raise AnvilError("anvil did not become ready in time")
                await asyncio.sleep(0.25)
        self.fork_block = fork_block

    async def reset(self, fork_block: int) -> None:
        await self.client.call("anvil_reset", [{"forking": {"jsonRpcUrl": self.fork_url, "blockNumber": fork_block}}])
        self.fork_block = fork_block

    async def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
        self.proc = None
        await self.client.close()
        self.client = self._new_client()

    def _new_client(self) -> RpcClient:
        return RpcClient(
            [f"http://127.0.0.1:{self.port}"],
            requests_per_second=1000,
            max_attempts=1,
            batch_size=50,
            timeout_s=self.request_timeout_s,
        )

    # ---------------------------------------------------------------- state

    async def snapshot(self) -> str:
        return await self.client.call("evm_snapshot")

    async def revert(self, snap_id: str) -> None:
        await self.client.call("evm_revert", [snap_id])

    async def set_balance(self, addr: str, wei: int) -> None:
        await self.client.call("anvil_setBalance", [addr, hex(wei)])

    async def set_storage(self, addr: str, slot: int, value: int) -> None:
        await self.client.call("anvil_setStorageAt", [addr, "0x" + slot.to_bytes(32, "big").hex(), "0x" + value.to_bytes(32, "big").hex()])

    async def get_storage(self, addr: str, slot: int) -> int:
        return int(await self.client.call("eth_getStorageAt", [addr, hex(slot), "latest"]), 16)

    async def balance_of(self, token: str, holder: str) -> int:
        raw = await self.client.eth_call(token, ERC20_BALANCE_OF.encode(holder))
        return ERC20_BALANCE_OF.decode_one(raw)

    async def fund_erc20(self, token: str, holder: str, amount: int, max_slot: int = 60) -> bool:
        """Set ``balanceOf(holder) = amount`` by locating the balances mapping slot.

        Tries Solidity layout keccak(holder . slot), Vyper layout
        keccak(slot . holder), and OpenZeppelin v5 namespaced storage.
        Returns False for tokens with non-standard accounting (rebasing,
        shares-based, etc.); callers must then fall back to flash funding.
        """
        holder_word = bytes.fromhex(holder[2:].lower().rjust(64, "0"))
        candidates: list[int] = []
        for slot in range(max_slot):
            slot_word = slot.to_bytes(32, "big")
            candidates.append(int.from_bytes(keccak(holder_word + slot_word), "big"))
            candidates.append(int.from_bytes(keccak(slot_word + holder_word), "big"))
        candidates.append(int.from_bytes(keccak(holder_word + _OZ5_ERC20_BALANCES_SLOT.to_bytes(32, "big")), "big"))
        for storage_slot in candidates:
            original = await self.get_storage(token, storage_slot)
            await self.set_storage(token, storage_slot, amount)
            try:
                if await self.balance_of(token, holder) == amount:
                    return True
            except RpcError:
                pass
            await self.set_storage(token, storage_slot, original)
        return False

    async def send(self, from_: str, to: str | None, data: str, value: int = 0, gas: int = 15_000_000) -> dict[str, Any]:
        tx: dict[str, Any] = {"from": from_, "data": data, "gas": hex(gas), "value": hex(value)}
        if to:
            tx["to"] = to
        tx_hash = await self.client.call("eth_sendTransaction", [tx])
        receipt = await self.client.call("eth_getTransactionReceipt", [tx_hash])
        if receipt is None:
            await self.client.call("evm_mine")
            receipt = await self.client.call("eth_getTransactionReceipt", [tx_hash])
        if receipt is None:
            raise AnvilError(f"no receipt for {tx_hash}")
        return receipt

    async def deploy(self, from_: str, bytecode: str, ctor_args: bytes = b"") -> str:
        data = bytecode + ctor_args.hex()
        receipt = await self.send(from_, None, data)
        if int(receipt["status"], 16) != 1 or not receipt.get("contractAddress"):
            raise AnvilError("contract deployment failed on fork")
        return to_checksum_address(receipt["contractAddress"])

    async def call_revert_data(self, from_: str, to: str, data: str, gas: int = 15_000_000) -> tuple[bool, str | None, RpcError | None]:
        """eth_call the tx on the fork. Returns (success, revert_data, error)."""
        try:
            await self.client.eth_call(to, data, "latest", from_=from_, gas=gas)
            return True, None, None
        except RpcError as exc:
            if exc.is_revert:
                return False, exc.revert_data, exc
            raise


def bytes_to_hex(b: bytes) -> str:
    return "0x" + b.hex()


def ensure_bytes(v: str | bytes) -> bytes:
    return hex_to_bytes(v) if isinstance(v, str) else v
