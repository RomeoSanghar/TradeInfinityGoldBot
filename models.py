"""
SQLite persistence for open trades and audit trail.
Compatible with Replit; file-based so it survives process restarts
as long as the Replit filesystem is persistent for the project.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional, List, Any
import json
import uuid


class Direction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class TradeStatus(str, Enum):
    OPEN = "OPEN"
    CLOSED_TP = "CLOSED_TP"
    CLOSED_SL = "CLOSED_SL"
    CLOSED_MANUAL = "CLOSED_MANUAL"
    CLOSED_AMBIGUOUS = "CLOSED_AMBIGUOUS"
    CLOSED_BREAKEVEN = "CLOSED_BREAKEVEN"


@dataclass
class Trade:
    trade_id: str
    symbol: str
    direction: str
    entry: float
    sl: float
    tp: float
    status: str
    created_at: str
    updated_at: str
    closed_at: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    telegram_message_id: Optional[int] = None
    last_price: Optional[float] = None
    last_price_ts: Optional[str] = None
    close_price: Optional[float] = None
    close_reason: Optional[str] = None
    notes: Optional[str] = None
    audit_log: str = "[]"  # JSON list of change records

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Trade":
        return cls(
            trade_id=row["trade_id"],
            symbol=row["symbol"],
            direction=row["direction"],
            entry=row["entry"],
            sl=row["sl"],
            tp=row["tp"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            closed_at=row["closed_at"],
            telegram_chat_id=row["telegram_chat_id"],
            telegram_message_id=row["telegram_message_id"],
            last_price=row["last_price"],
            last_price_ts=row["last_price_ts"],
            close_price=row["close_price"],
            close_reason=row["close_reason"],
            notes=row["notes"],
            audit_log=row["audit_log"] or "[]",
        )


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class TradeRepository:
    def __init__(self, db_path: str = "trades.db"):
        self.db_path = db_path
        self._ensure_schema()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trades (
                    trade_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    entry REAL NOT NULL,
                    sl REAL NOT NULL,
                    tp REAL NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    closed_at TEXT,
                    telegram_chat_id TEXT,
                    telegram_message_id INTEGER,
                    last_price REAL,
                    last_price_ts TEXT,
                    close_price REAL,
                    close_reason TEXT,
                    notes TEXT,
                    audit_log TEXT DEFAULT '[]'
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS scheduler_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status)"
            )

    def create_trade(
        self,
        symbol: str,
        direction: str,
        entry: float,
        sl: float,
        tp: float,
        telegram_chat_id: Optional[str] = None,
        telegram_message_id: Optional[int] = None,
        notes: Optional[str] = None,
    ) -> Trade:
        trade_id = f"TI-{uuid.uuid4().hex[:8].upper()}"
        now = _utcnow()
        trade = Trade(
            trade_id=trade_id,
            symbol=symbol,
            direction=direction.upper(),
            entry=entry,
            sl=sl,
            tp=tp,
            status=TradeStatus.OPEN.value,
            created_at=now,
            updated_at=now,
            telegram_chat_id=telegram_chat_id,
            telegram_message_id=telegram_message_id,
            notes=notes,
            audit_log=json.dumps(
                [{"ts": now, "event": "created", "by": "admin"}]
            ),
        )
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO trades (
                    trade_id, symbol, direction, entry, sl, tp, status,
                    created_at, updated_at, telegram_chat_id, telegram_message_id,
                    notes, audit_log
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade.trade_id,
                    trade.symbol,
                    trade.direction,
                    trade.entry,
                    trade.sl,
                    trade.tp,
                    trade.status,
                    trade.created_at,
                    trade.updated_at,
                    trade.telegram_chat_id,
                    trade.telegram_message_id,
                    trade.notes,
                    trade.audit_log,
                ),
            )
        return trade

    def get_trade(self, trade_id: str) -> Optional[Trade]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM trades WHERE trade_id = ?", (trade_id,)
            ).fetchone()
            return Trade.from_row(row) if row else None

    def list_open_trades(self) -> List[Trade]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM trades WHERE status = ? ORDER BY created_at",
                (TradeStatus.OPEN.value,),
            ).fetchall()
            return [Trade.from_row(r) for r in rows]

    def list_recent_trades(self, limit: int = 20) -> List[Trade]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM trades ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [Trade.from_row(r) for r in rows]

    def update_trade_message_ids(
        self, trade_id: str, chat_id: str, message_id: int
    ) -> None:
        now = _utcnow()
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE trades
                SET telegram_chat_id = ?, telegram_message_id = ?, updated_at = ?
                WHERE trade_id = ?
                """,
                (str(chat_id), message_id, now, trade_id),
            )

    def update_price(self, trade_id: str, price: float, price_ts: str) -> None:
        now = _utcnow()
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE trades
                SET last_price = ?, last_price_ts = ?, updated_at = ?
                WHERE trade_id = ? AND status = ?
                """,
                (price, price_ts, now, trade_id, TradeStatus.OPEN.value),
            )

    def append_audit(self, trade_id: str, event: str, details: dict | None = None) -> None:
        trade = self.get_trade(trade_id)
        if not trade:
            return
        log = json.loads(trade.audit_log or "[]")
        entry = {"ts": _utcnow(), "event": event}
        if details:
            entry.update(details)
        log.append(entry)
        with self._conn() as conn:
            conn.execute(
                "UPDATE trades SET audit_log = ?, updated_at = ? WHERE trade_id = ?",
                (json.dumps(log), _utcnow(), trade_id),
            )

    def close_trade(
        self,
        trade_id: str,
        status: str,
        close_price: Optional[float] = None,
        close_reason: Optional[str] = None,
        by: str = "system",
    ) -> Optional[Trade]:
        now = _utcnow()
        self.append_audit(
            trade_id,
            "closed",
            {"status": status, "close_price": close_price, "reason": close_reason, "by": by},
        )
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE trades
                SET status = ?, closed_at = ?, close_price = ?, close_reason = ?, updated_at = ?
                WHERE trade_id = ? AND status = ?
                """,
                (
                    status,
                    now,
                    close_price,
                    close_reason,
                    now,
                    trade_id,
                    TradeStatus.OPEN.value,
                ),
            )
        return self.get_trade(trade_id)

    def set_breakeven(self, trade_id: str, new_sl: float, by: str = "admin") -> Optional[Trade]:
        trade = self.get_trade(trade_id)
        if not trade or trade.status != TradeStatus.OPEN.value:
            return None
        old_sl = trade.sl
        self.append_audit(
            trade_id,
            "breakeven",
            {"old_sl": old_sl, "new_sl": new_sl, "by": by},
        )
        now = _utcnow()
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE trades SET sl = ?, updated_at = ? WHERE trade_id = ?
                """,
                (new_sl, now, trade_id),
            )
        return self.get_trade(trade_id)

    def get_scheduler_flag(self, key: str) -> Optional[str]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM scheduler_state WHERE key = ?", (key,)
            ).fetchone()
            return row["value"] if row else None

    def set_scheduler_flag(self, key: str, value: str) -> None:
        now = _utcnow()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO scheduler_state (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, value, now),
            )
