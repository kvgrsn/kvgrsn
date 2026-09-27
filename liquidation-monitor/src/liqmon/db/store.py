"""SQLite state store.

Holds the indexer checkpoints (with block hashes for reorg detection), the
raw position-discovery events, the derived set of known positions with their
last observed health, and the opportunities produced by the scanner.

SQLite keeps the tool zero-setup for a single machine; the access pattern is
small and the interface is narrow enough to port to PostgreSQL later.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS checkpoints (
    stream          TEXT PRIMARY KEY,
    last_block      INTEGER NOT NULL,
    updated_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS block_hashes (
    stream          TEXT NOT NULL,
    block_number    INTEGER NOT NULL,
    block_hash      TEXT NOT NULL,
    PRIMARY KEY (stream, block_number)
);

CREATE TABLE IF NOT EXISTS position_events (
    protocol_id     TEXT NOT NULL,
    account         TEXT NOT NULL,
    kind            TEXT NOT NULL,
    block_number    INTEGER NOT NULL,
    block_hash      TEXT NOT NULL,
    tx_hash         TEXT NOT NULL,
    log_index       INTEGER NOT NULL,
    PRIMARY KEY (protocol_id, tx_hash, log_index)
);
CREATE INDEX IF NOT EXISTS idx_events_block ON position_events (protocol_id, block_number);
CREATE INDEX IF NOT EXISTS idx_events_account ON position_events (protocol_id, account);

CREATE TABLE IF NOT EXISTS positions (
    protocol_id         TEXT NOT NULL,
    account             TEXT NOT NULL,
    first_seen_block    INTEGER NOT NULL,
    last_event_block    INTEGER NOT NULL,
    status              TEXT NOT NULL DEFAULT 'unknown',
    health_factor       REAL,
    collateral_usd      REAL,
    debt_usd            REAL,
    last_checked_block  INTEGER,
    last_checked_at     REAL,
    PRIMARY KEY (protocol_id, account)
);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions (protocol_id, status);

CREATE TABLE IF NOT EXISTS opportunities (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    protocol_id         TEXT NOT NULL,
    chain               TEXT NOT NULL,
    account             TEXT NOT NULL,
    collateral_asset    TEXT NOT NULL,
    debt_asset          TEXT NOT NULL,
    block_number        INTEGER NOT NULL,
    created_at          REAL NOT NULL,
    sim_status          TEXT NOT NULL,
    expected_profit_usd REAL,
    payload             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_opps_created ON opportunities (created_at);
"""


@dataclass(frozen=True)
class PositionEvent:
    protocol_id: str
    account: str
    kind: str
    block_number: int
    block_hash: str
    tx_hash: str
    log_index: int


@dataclass(frozen=True)
class KnownPosition:
    protocol_id: str
    account: str
    first_seen_block: int
    last_event_block: int
    status: str
    health_factor: float | None


class Store:
    def __init__(self, path: str | Path):
        path = Path(path)
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------ checkpoints

    def get_checkpoint(self, stream: str) -> int | None:
        row = self.conn.execute("SELECT last_block FROM checkpoints WHERE stream=?", (stream,)).fetchone()
        return row[0] if row else None

    def set_checkpoint(self, stream: str, block: int, block_hash: str | None, keep_hashes: int) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO checkpoints(stream,last_block,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(stream) DO UPDATE SET last_block=excluded.last_block, updated_at=excluded.updated_at",
                (stream, block, time.time()),
            )
            if block_hash:
                self.conn.execute(
                    "INSERT OR REPLACE INTO block_hashes(stream,block_number,block_hash) VALUES(?,?,?)",
                    (stream, block, block_hash),
                )
                # Keep only the most recent hashes.
                self.conn.execute(
                    "DELETE FROM block_hashes WHERE stream=? AND block_number NOT IN "
                    "(SELECT block_number FROM block_hashes WHERE stream=? ORDER BY block_number DESC LIMIT ?)",
                    (stream, stream, keep_hashes),
                )

    def recent_block_hashes(self, stream: str) -> list[tuple[int, str]]:
        return self.conn.execute(
            "SELECT block_number, block_hash FROM block_hashes WHERE stream=? ORDER BY block_number DESC",
            (stream,),
        ).fetchall()

    def rollback(self, stream: str, protocol_id: str, to_block: int) -> int:
        """Forget everything indexed after ``to_block`` (reorg recovery).

        Returns the number of discarded events.
        """
        with self.conn:
            cur = self.conn.execute(
                "DELETE FROM position_events WHERE protocol_id=? AND block_number>?", (protocol_id, to_block)
            )
            removed = cur.rowcount
            # Positions whose only evidence was discarded disappear; the rest
            # get their last_event_block recomputed.
            self.conn.execute(
                "DELETE FROM positions WHERE protocol_id=? AND account NOT IN "
                "(SELECT DISTINCT account FROM position_events WHERE protocol_id=?)",
                (protocol_id, protocol_id),
            )
            self.conn.execute(
                "UPDATE positions SET last_event_block=(SELECT MAX(block_number) FROM position_events e "
                "WHERE e.protocol_id=positions.protocol_id AND e.account=positions.account), "
                "first_seen_block=(SELECT MIN(block_number) FROM position_events e "
                "WHERE e.protocol_id=positions.protocol_id AND e.account=positions.account) "
                "WHERE protocol_id=?",
                (protocol_id,),
            )
            self.conn.execute("DELETE FROM block_hashes WHERE stream=? AND block_number>?", (stream, to_block))
            self.conn.execute(
                "UPDATE checkpoints SET last_block=?, updated_at=? WHERE stream=?", (to_block, time.time(), stream)
            )
        return removed

    # ---------------------------------------------------------------- events

    def add_events(self, events: Iterable[PositionEvent]) -> int:
        events = list(events)
        if not events:
            return 0
        with self.conn:
            self.conn.executemany(
                "INSERT OR IGNORE INTO position_events(protocol_id,account,kind,block_number,block_hash,tx_hash,log_index) "
                "VALUES(?,?,?,?,?,?,?)",
                [(e.protocol_id, e.account, e.kind, e.block_number, e.block_hash, e.tx_hash, e.log_index) for e in events],
            )
            for e in events:
                self.conn.execute(
                    "INSERT INTO positions(protocol_id,account,first_seen_block,last_event_block,status) "
                    "VALUES(?,?,?,?, 'unknown') ON CONFLICT(protocol_id,account) DO UPDATE SET "
                    "last_event_block=MAX(last_event_block, excluded.last_event_block), "
                    "first_seen_block=MIN(first_seen_block, excluded.first_seen_block), "
                    # a fresh borrow re-activates a closed position
                    "status=CASE WHEN excluded.last_event_block > positions.last_event_block "
                    "AND positions.status='closed' THEN 'unknown' ELSE positions.status END",
                    (e.protocol_id, e.account, e.block_number, e.block_number),
                )
        return len(events)

    # ------------------------------------------------------------- positions

    def positions(self, protocol_id: str, include_closed: bool = False) -> list[KnownPosition]:
        q = "SELECT protocol_id,account,first_seen_block,last_event_block,status,health_factor FROM positions WHERE protocol_id=?"
        if not include_closed:
            q += " AND status!='closed'"
        return [KnownPosition(*row) for row in self.conn.execute(q, (protocol_id,)).fetchall()]

    def update_position_health(
        self,
        protocol_id: str,
        rows: Iterable[tuple[str, str, float | None, float, float]],
        block: int,
    ) -> None:
        """rows: (account, status, health_factor, collateral_usd, debt_usd)"""
        now = time.time()
        with self.conn:
            self.conn.executemany(
                "UPDATE positions SET status=?, health_factor=?, collateral_usd=?, debt_usd=?, "
                "last_checked_block=?, last_checked_at=? WHERE protocol_id=? AND account=?",
                [(st, hf, col, debt, block, now, protocol_id, acct) for acct, st, hf, col, debt in rows],
            )

    def position_counts(self, protocol_id: str) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) FROM positions WHERE protocol_id=? GROUP BY status", (protocol_id,)
        ).fetchall()
        return {status: n for status, n in rows}

    # --------------------------------------------------------- opportunities

    def add_opportunity(
        self,
        protocol_id: str,
        chain: str,
        account: str,
        collateral_asset: str,
        debt_asset: str,
        block_number: int,
        sim_status: str,
        expected_profit_usd: float | None,
        payload: dict[str, Any],
    ) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO opportunities(protocol_id,chain,account,collateral_asset,debt_asset,block_number,"
                "created_at,sim_status,expected_profit_usd,payload) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    protocol_id,
                    chain,
                    account,
                    collateral_asset,
                    debt_asset,
                    block_number,
                    time.time(),
                    sim_status,
                    expected_profit_usd,
                    json.dumps(payload, default=str),
                ),
            )
            return int(cur.lastrowid)

    def get_opportunity(self, opp_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT id,protocol_id,chain,account,collateral_asset,debt_asset,block_number,created_at,sim_status,"
            "expected_profit_usd,payload FROM opportunities WHERE id=?",
            (opp_id,),
        ).fetchone()
        if not row:
            return None
        keys = [
            "id",
            "protocol_id",
            "chain",
            "account",
            "collateral_asset",
            "debt_asset",
            "block_number",
            "created_at",
            "sim_status",
            "expected_profit_usd",
        ]
        out = dict(zip(keys, row[:-1]))
        out["payload"] = json.loads(row[-1])
        return out
