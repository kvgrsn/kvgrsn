"""Terminal dashboard (rich)."""

from __future__ import annotations

from rich import box
from rich.console import Console, Group
from rich.table import Table
from rich.text import Text

from ..engine.scanner import CycleReport
from ..models import Opportunity, SimStatus

CHAIN_LABEL = {"bsc": "BSC", "avalanche": "AVAX"}
PROTOCOL_LABEL = {"aave_v3_bsc": "Aave V3", "aave_v3_avalanche": "Aave V3"}
LIQ_LABEL = {
    "repay_and_seize": "Repay&Seize",
    "dutch_auction": "Dutch Auction",
    "health_based_discount": "HF Dutch",
}
SIM_STYLE = {
    SimStatus.SUCCESS: "bold green",
    SimStatus.REVERT: "red",
    SimStatus.ERROR: "yellow",
    SimStatus.SKIPPED: "dim",
    SimStatus.PENDING: "dim",
}

COLUMNS = [
    "CHAIN", "PROTOCOL", "POSITION", "COLLATERAL", "DEBT", "HF / CR", "LIQ TYPE", "AUCTION ID",
    "DISCOUNT", "CAPITAL REQ", "FLASH", "EST GAS", "EXP PROFIT", "SIM STATUS",
]  # fmt: skip


def short(addr: str) -> str:
    return f"{addr[:6]}…{addr[-4:]}"


def usd(v: float | None) -> str:
    if v is None:
        return "-"
    sign = "-" if v < 0 else ""
    return f"{sign}${abs(v):,.2f}"


def opportunity_row(o: Opportunity) -> list[Text | str]:
    q = o.quote
    est = o.estimate
    gas_usd = None
    if o.simulation.gas_used and est:
        gas_usd = o.simulation.gas_used * est.gas_price_wei / 1e18 * est.native_price_usd
    elif est:
        gas_usd = est.gas_cost_usd
    capital = usd(q.debt_value_usd) if o.funding is None or o.funding.mode == "wallet" else f"{usd(q.debt_value_usd)} (flash)"
    flash = "-"
    if o.funding is not None:
        flash = "YES" if o.funding.mode == "flash" else "WALLET"
    elif o.rejected_reason and "flash" in o.rejected_reason:
        flash = "NO"
    status = o.simulation.status
    status_text = Text(status.value, style=SIM_STYLE[status])
    if status == SimStatus.REVERT and o.simulation.revert_reason:
        status_text.append(f" {o.simulation.revert_reason[:40]}", style="red dim")
    elif o.rejected_reason:
        status_text.append(" (rejected)", style="dim")
    profit = o.profit_usd
    profit_text = Text(usd(profit), style="green" if profit and profit > 0 else "red")
    return [
        CHAIN_LABEL.get(q.chain, q.chain),
        PROTOCOL_LABEL.get(q.protocol_id, q.protocol_id),
        short(q.account),
        q.collateral_symbol,
        q.debt_symbol,
        f"{o.health_factor:.4f}" if o.health_factor is not None else "-",
        LIQ_LABEL.get(q.liquidation_type.value, q.liquidation_type.value),
        q.auction_id or "-",
        f"{q.auction_discount * 100:.2f}%" if q.auction_discount is not None else "-",
        capital,
        flash,
        usd(gas_usd),
        profit_text,
        status_text,
    ]


def render_report(reports: list[CycleReport], min_profit_usd: float) -> Group:
    table = Table(box=box.SIMPLE_HEAVY, header_style="bold", expand=False)
    for c in COLUMNS:
        table.add_column(c, no_wrap=True)
    n_opps = 0
    for r in reports:
        for o in r.opportunities:
            table.add_row(*opportunity_row(o))
            n_opps += 1
    watch = Table(box=box.SIMPLE, header_style="bold", title="Watchlist (HF below watch threshold, not yet liquidatable)")
    for c in ("CHAIN", "PROTOCOL", "POSITION", "HF", "COLLATERAL $", "DEBT $"):
        watch.add_column(c, no_wrap=True)
    watch_rows = sorted(((r.chain, s) for r in reports for s in r.watch), key=lambda x: x[1].health_factor or 9)
    for chain, s in watch_rows[:25]:
        watch.add_row(
            CHAIN_LABEL.get(chain, chain),
            PROTOCOL_LABEL.get(s.protocol_id, s.protocol_id),
            s.account,
            f"{s.health_factor:.4f}",
            usd(s.total_collateral_usd),
            usd(s.total_debt_usd),
        )
    header = Text()
    for r in reports:
        header.append(
            f"{CHAIN_LABEL.get(r.chain, r.chain)} block {r.block}: {r.positions_checked} positions, "
            f"{r.liquidatable} liquidatable, {len(r.blocked)} HF<1 but blocked, {len(r.watch)} on watch "
            f"({r.duration_s:.1f}s)\n"
        )
    header.append(f"Min profit threshold {usd(min_profit_usd)}. DRY RUN: scanner never broadcasts.\n", style="dim")
    parts: list = [header]
    parts.append(table if n_opps else Text("No liquidatable opportunities this cycle.", style="dim"))
    if watch_rows:
        parts.append(watch)
    blocked = [(r.chain, s, e) for r in reports for s, e in r.blocked]
    if blocked:
        bt = Table(box=box.SIMPLE, title="HF < 1 but not liquidatable now", header_style="bold")
        for c in ("CHAIN", "POSITION", "HF", "COLLATERAL $", "DEBT $", "BLOCKER"):
            bt.add_column(c)
        for chain, s, e in blocked[:15]:
            bt.add_row(
                CHAIN_LABEL.get(chain, chain),
                short(s.account),
                f"{s.health_factor:.4f}",
                usd(s.total_collateral_usd),
                usd(s.total_debt_usd),
                "; ".join(e.blockers)[:90],
            )
        parts.append(bt)
    errs = [e for r in reports for e in r.errors]
    if errs:
        parts.append(Text("\n".join(f"! {e}" for e in errs[:10]), style="yellow"))
    return Group(*parts)


def print_report(reports: list[CycleReport], min_profit_usd: float, console: Console | None = None) -> None:
    (console or Console()).print(render_report(reports, min_profit_usd))
