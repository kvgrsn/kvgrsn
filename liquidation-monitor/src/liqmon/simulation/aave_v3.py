"""Full-path fork simulation for Aave V3 liquidations.

Reproduces, in one transaction on an anvil fork of the scan block:
flash loan (or pre-funded capital) -> liquidationCall -> collateral receipt
-> DEX swap -> flash repayment -> profit check.
Reports success/revert, gas used, executor token deltas, the protocol's own
LiquidationCall event amounts, and profit net of gas.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from eth_utils import to_checksum_address

from ..models import SimStatus, SimulationResult
from ..protocols.aave_v3 import abi as A
from ..rpc.abi import Fn, decode_revert, hex_to_bytes
from .simulator import ForkSimulator

if TYPE_CHECKING:
    from ..protocols.aave_v3.adapter import AaveV3Adapter
    from ..protocols.base import ExecutionPlan

PARAMS_TUPLE = "(address,address,address,uint256,uint8,address,bytes,uint256,uint256)"
EXEC_FLASH = Fn(f"executeWithFlashLoan({PARAMS_TUPLE},uint256)")
EXEC_CAPITAL = Fn(f"executeWithCapital({PARAMS_TUPLE})")


def executor_params(p: dict) -> tuple:
    return (
        to_checksum_address(p["collateralAsset"]),
        to_checksum_address(p["debtAsset"]),
        to_checksum_address(p["borrower"]),
        int(p["debtToCover"]),
        int(p["swapKind"]),
        to_checksum_address(p["router"]),
        hex_to_bytes(p["path"]),
        int(p["minAmountOut"]),
        int(p["minProfit"]),
    )


def executor_calldata(p: dict) -> str:
    params = executor_params(p)
    if p["fundingMode"] == "flash":
        return EXEC_FLASH.encode(params, int(p["flashAmount"]))
    return EXEC_CAPITAL.encode(params)


async def simulate_aave_v3_liquidation(
    adapter: "AaveV3Adapter",
    plan: "ExecutionPlan",
    sim: ForkSimulator,
    *,
    deployed_executor: str | None = None,
    sender: str | None = None,
) -> SimulationResult:
    """Simulate on the fork.

    By default a fresh executor is deployed on the fork (owned by the
    simulation identity). Pass ``deployed_executor``/``sender`` to simulate the
    exact contract and owner that would broadcast (used by the executor module).
    """
    fork = sim.fork
    p = plan.params
    debt = to_checksum_address(p["debtAsset"])
    collateral = to_checksum_address(p["collateralAsset"])
    snap = await fork.snapshot()
    try:
        if deployed_executor:
            executor = to_checksum_address(deployed_executor)
            caller = to_checksum_address(sender or sim.owner)
            await fork.set_balance(caller, 10**24)
        else:
            caller = sim.owner
            executor = await sim.deploy(
                "AaveV3LiquidationExecutor", abi_encode(["address", "address"], [adapter.pool, sim.owner])
            )
        if p["fundingMode"] == "wallet" and not deployed_executor:
            if not await fork.fund_erc20(debt, executor, int(p["flashAmount"])):
                return SimulationResult(
                    status=SimStatus.ERROR,
                    block_number=fork.fork_block,
                    detail=f"cannot locate balance slot of {plan.quote.debt_symbol} to fund wallet-mode simulation",
                )
        data = executor_calldata(p)
        tokens = [debt] if debt == collateral else [debt, collateral]
        before = {t: await fork.balance_of(t, executor) for t in tokens}

        ok, revert_data, err = await fork.call_revert_data(caller, executor, data)
        if not ok:
            reason = decode_revert(revert_data) if revert_data else (err.message if err else "reverted")
            return SimulationResult(status=SimStatus.REVERT, block_number=fork.fork_block, revert_reason=reason)

        receipt = await fork.send(caller, executor, data)
        gas_used = int(receipt["gasUsed"], 16)
        if int(receipt["status"], 16) != 1:
            return SimulationResult(
                status=SimStatus.REVERT,
                block_number=fork.fork_block,
                gas_used=gas_used,
                revert_reason="reverted on inclusion (eth_call succeeded)",
            )
        after = {t: await fork.balance_of(t, executor) for t in tokens}
        deltas = {t: after[t] - before[t] for t in tokens}

        debt_repaid = collateral_seized = None
        for lg in receipt.get("logs", []):
            if (
                lg["address"].lower() == adapter.pool.lower()
                and lg["topics"]
                and lg["topics"][0].lower() == A.LIQUIDATION_CALL_TOPIC.lower()
            ):
                dtc, seized, _liquidator, _recv = abi_decode(
                    ["uint256", "uint256", "address", "bool"], hex_to_bytes(lg["data"])
                )
                debt_repaid, collateral_seized = int(dtc), int(seized)

        profit_units = deltas[debt]
        debt_usd = adapter.usd_price(debt) or 0.0
        gas_cost_native = gas_used * sim.gas_price_wei / 1e18
        profit_usd = profit_units * debt_usd / 10**plan.quote.debt_decimals - gas_cost_native * sim.native_usd
        return SimulationResult(
            status=SimStatus.SUCCESS,
            block_number=fork.fork_block,
            gas_used=gas_used,
            token_deltas=deltas,
            debt_repaid=debt_repaid,
            collateral_seized=collateral_seized,
            profit_debt_units=profit_units,
            profit_usd=profit_usd,
        )
    finally:
        await fork.revert(snap)
