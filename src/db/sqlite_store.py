"""SQLite WAL-mode database store for Agent 4 (Execution & HITL Queue).

Supports both the extended schema (`action`, `shares`, `bull_case`, `devils_advocate`, `x_request_id`)
and the root script columns (`side`, `amount`, `gemini_reason`) so `telegram_hitl_bot.py`,
`stream_execution_bridge.py`, and `src/hitl/order_queue.py` interoperate on the same table.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from config.settings import settings

PENDING_ORDERS_DDL = """
CREATE TABLE IF NOT EXISTS pending_orders (
    order_id TEXT PRIMARY KEY,
    x_request_id TEXT,
    ticker TEXT NOT NULL,
    action TEXT NOT NULL DEFAULT 'BUY',
    side TEXT NOT NULL DEFAULT 'BUY',
    order_type TEXT NOT NULL DEFAULT 'LIMIT',
    shares INTEGER NOT NULL DEFAULT 1,
    amount INTEGER NOT NULL DEFAULT 1,
    signal_close_price REAL NOT NULL DEFAULT 0.0,
    limit_price REAL NOT NULL,
    stop_loss_price REAL,
    max_gap_up_pct REAL NOT NULL DEFAULT 0.015,
    max_gap_down_pct REAL NOT NULL DEFAULT -0.020,
    strategy_name TEXT NOT NULL DEFAULT 'rsi_mean_reversion',
    horizon_days INTEGER NOT NULL DEFAULT 20,
    priority_rank REAL,
    llm_provider TEXT,
    llm_recommendation TEXT,
    llm_confidence REAL,
    risk_score TEXT,
    earnings_blackout_flag INTEGER NOT NULL DEFAULT 0,
    bull_case TEXT,
    devils_advocate TEXT,
    gemini_reason TEXT DEFAULT '',
    requires_hitl INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP,
    status TEXT NOT NULL DEFAULT 'PENDING',
    telegram_message_id INTEGER,
    protective_stop_request_id TEXT,
    protective_stop_price REAL,
    protective_stop_status TEXT
    reserved_cash_nok REAL NOT NULL DEFAULT 0.0,
    broker_order_id TEXT,
    fill_status TEXT NOT NULL DEFAULT 'UNFILLED',
    fill_price REAL,
    fill_qty INTEGER
);

CREATE INDEX IF NOT EXISTS idx_pending_orders_status ON pending_orders(status);
CREATE INDEX IF NOT EXISTS idx_pending_orders_ticker ON pending_orders(ticker);
"""


@contextmanager
def get_sqlite_conn(db_path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    """Provide a transactional SQLite connection configured with WAL mode and busy timeout."""
    target_path = Path(db_path) if db_path is not None else settings.sqlite_db_path
    target_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_sqlite_db(db_path: Path | str | None = None) -> Path:
    """Initialize the SQLite database in WAL mode and ensure all compatible columns exist."""
    target_path = Path(db_path) if db_path is not None else settings.sqlite_db_path
    with get_sqlite_conn(target_path) as conn:
        conn.executescript(PENDING_ORDERS_DDL)
        existing_cols = {
            row[1] for row in conn.execute("PRAGMA table_xinfo(pending_orders);").fetchall()
        }
        if "gemini_reason" not in existing_cols:
            conn.execute("ALTER TABLE pending_orders ADD COLUMN gemini_reason TEXT DEFAULT '';")
        if "side" not in existing_cols:
            conn.execute("ALTER TABLE pending_orders ADD COLUMN side TEXT DEFAULT 'BUY';")
        if "amount" not in existing_cols:
            conn.execute("ALTER TABLE pending_orders ADD COLUMN amount INTEGER DEFAULT 1;")
        if "protective_stop_request_id" not in existing_cols:
            conn.execute("ALTER TABLE pending_orders ADD COLUMN protective_stop_request_id TEXT;")
        if "protective_stop_price" not in existing_cols:
            conn.execute("ALTER TABLE pending_orders ADD COLUMN protective_stop_price REAL;")
        if "protective_stop_status" not in existing_cols:
            conn.execute("ALTER TABLE pending_orders ADD COLUMN protective_stop_status TEXT;")
        if "reserved_cash_nok" not in existing_cols:
            conn.execute("ALTER TABLE pending_orders ADD COLUMN reserved_cash_nok REAL NOT NULL DEFAULT 0.0;")
        if "broker_order_id" not in existing_cols:
            conn.execute("ALTER TABLE pending_orders ADD COLUMN broker_order_id TEXT;")
        if "fill_status" not in existing_cols:
            conn.execute("ALTER TABLE pending_orders ADD COLUMN fill_status TEXT NOT NULL DEFAULT 'UNFILLED';")
        if "fill_price" not in existing_cols:
            conn.execute("ALTER TABLE pending_orders ADD COLUMN fill_price REAL;")
        if "fill_qty" not in existing_cols:
            conn.execute("ALTER TABLE pending_orders ADD COLUMN fill_qty INTEGER;")
    return target_path
