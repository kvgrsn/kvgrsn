"""Optional alerts. Fired only when simulation == SUCCESS and the simulated
profit clears ``alert_min_profit_usd``. Credentials come from env only."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time

import httpx

from ..config import Settings
from ..models import Opportunity, SimStatus

log = logging.getLogger(__name__)


def should_alert(o: Opportunity, settings: Settings) -> bool:
    return (
        o.simulation.status == SimStatus.SUCCESS
        and o.rejected_reason is None
        and o.profit_usd is not None
        and o.profit_usd > settings.alert_min_profit_usd
    )


def format_alert(o: Opportunity) -> str:
    q = o.quote
    return (
        f"[{q.chain.upper()}] {q.protocol_id} liquidation SIM SUCCESS\n"
        f"borrower {q.account} HF {o.health_factor:.4f}\n"
        f"repay {q.expected_debt_repaid / 10**q.debt_decimals:,.4f} {q.debt_symbol} -> "
        f"seize {q.expected_collateral_out / 10**q.collateral_decimals:,.4f} {q.collateral_symbol}\n"
        f"simulated profit ${o.profit_usd:,.2f} (gas {o.simulation.gas_used}) block {o.simulation.block_number}\n"
        f"opportunity id {o.opportunity_id} (DRY RUN; executor is separate)"
    )


class Alerter:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.telegram_token = os.environ.get("LIQMON_TELEGRAM_BOT_TOKEN")
        self.telegram_chat = os.environ.get("LIQMON_TELEGRAM_CHAT_ID")
        self.discord_webhook = os.environ.get("LIQMON_DISCORD_WEBHOOK_URL")
        self._sent: dict[tuple[str, str, str, str], float] = {}

    def _key(self, o: Opportunity) -> tuple[str, str, str, str]:
        return (o.quote.protocol_id, o.quote.account.lower(), o.quote.collateral_asset, o.quote.debt_asset)

    async def maybe_alert(self, opps: list[Opportunity]) -> int:
        sent = 0
        now = time.time()
        for o in opps:
            if not should_alert(o, self.settings):
                continue
            key = self._key(o)
            if now - self._sent.get(key, 0) < self.settings.alert_cooldown_s:
                continue
            self._sent[key] = now
            await self._send(format_alert(o))
            sent += 1
        return sent

    async def _send(self, text: str) -> None:
        async with httpx.AsyncClient(timeout=10) as http:
            if self.telegram_token and self.telegram_chat:
                try:
                    await http.post(
                        f"https://api.telegram.org/bot{self.telegram_token}/sendMessage",
                        json={"chat_id": self.telegram_chat, "text": text},
                    )
                except httpx.HTTPError as exc:
                    log.warning("telegram alert failed: %s", type(exc).__name__)
            if self.discord_webhook:
                try:
                    await http.post(self.discord_webhook, json={"content": text})
                except httpx.HTTPError as exc:
                    log.warning("discord alert failed: %s", type(exc).__name__)
        if self.settings.alert_desktop and shutil.which("notify-send"):
            subprocess.run(["notify-send", "liqmon", text], check=False)
