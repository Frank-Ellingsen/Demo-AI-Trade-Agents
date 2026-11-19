"""Agent 4: Order Queue state machine in SQLite WAL with 14h auto-expiry and x-request-id idempotency."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from config.settings import settings
from src.db.sqlite_store import get_sqlite_conn, init_sqlite_db


def enqueue_order_proposal(
    ticker: str,
    action: str,
    shares: int,
    signal_close_price: float,
    limit_price: float,
    stop_loss_price: float | None,
    strategy_name: str,
    horizon_days: int = 20,
    priority_rank: float = 0.0,
    llm_provider: str = "ollama",
    llm_recommendation: str = "GO",
    llm_confidence: float = 0.8,
    risk_score: str = "LOW",
    earnings_blackout_flag: bool = False,
    bull_case: str = "",
    devils_advocate: str = "",
    expiry_hours: int | None = None,
    order_id: str | None = None,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    """Insert a candidate order into SQLite WAL queue."""
    init_sqlite_db(db_path)
    action_upper = action.upper()
    if action_upper not in {"BUY", "SELL"}:
        raise ValueError("action must be 'BUY' or 'SELL'")

    oid = order_id or f"ord_{uuid.uuid4().hex[:12]}"
    x_request_id = f"saxo_{uuid.uuid4().hex[:16]}"
    now_utc = datetime.now(timezone.utc)
    ttl_hours = expiry_hours if expiry_hours is not None else settings.hitl_expiry_hours
    expires_at = (now_utc + timedelta(hours=ttl_hours)).isoformat()

    is_buy = action_upper == "BUY"
    requires_hitl = 1 if is_buy else 0
    initial_status = "PENDING" if is_buy else "AUTO_APPROVED"
    combined_reason = f"{bull_case} | Risk: {devils_advocate}".strip(" |")

    with get_sqlite_conn(db_path) as conn:
        cols = {row[1]: row[5] for row in conn.execute("PRAGMA table_info(pending_orders);").fetchall()}
        conn.execute(
            """
            INSERT OR REPLACE INTO pending_orders (
                order_id, x_request_id, ticker, action, order_type, shares,
                signal_close_price, limit_price, stop_loss_price,
                strategy_name, horizon_days, priority_rank,
                llm_provider, llm_recommendation, llm_confidence, risk_score,
                earnings_blackout_flag, bull_case, devils_advocate, gemini_reason,
                requires_hitl, created_at, expires_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                oid,
                x_request_id,
                ticker,
                action_upper,
                "LIMIT" if is_buy else "STOP_EXIT",
                shares,
                signal_close_price,
                limit_price,
                stop_loss_price,
                strategy_name,
                horizon_days,
                priority_rank,
                llm_provider,
                llm_recommendation,
                llm_confidence,
                risk_score,
                1 if earnings_blackout_flag else 0,
                bull_case,
                devils_advocate,
                combined_reason,
                requires_hitl,
                now_utc.isoformat(),
                expires_at,
                initial_status,
            ),
        )
        reserved_cash = float(shares * limit_price) if is_buy else 0.0
        conn.execute(
            "UPDATE pending_orders SET reserved_cash_nok = ? WHERE order_id = ?;",
            (reserved_cash, oid),
        )
        # If `side` / `amount` were added via ALTER TABLE (non-virtual), sync them explicitly
        try:
            conn.execute(
                "UPDATE pending_orders SET side = ?, amount = ? WHERE order_id = ?;",
                (action_upper, shares, oid),
            )
        except Exception:
            pass

    return {
        "order_id": oid,
        "x_request_id": x_request_id,
        "status": initial_status,
        "requires_hitl": bool(requires_hitl),
        "expires_at": expires_at,
    }


def expire_stale_orders(reference_time: datetime | None = None, db_path: Path | str | None = None) -> int:
    """Transition all PENDING orders past their 14-hour expires_at timestamp to EXPIRED."""
    ref_iso = (reference_time or datetime.now(timezone.utc)).isoformat()
    with get_sqlite_conn(db_path) as conn:
        cur = conn.execute(
            """
            UPDATE pending_orders
            SET status = 'EXPIRED'
            WHERE status = 'PENDING'
              AND expires_at IS NOT NULL
              AND expires_at <= ?;
            """,
            (ref_iso,),
        )
        conn.execute(
            """
            UPDATE pending_orders
            SET reserved_cash_nok = 0.0
            WHERE status = 'EXPIRED';
            """
        )
        return cur.rowcount


def update_order_status(order_id: str, new_status: str, db_path: Path | str | None = None) -> bool:
    """Update order status ('APPROVED', 'REJECTED', 'REJECTED_BY_USER', 'CANCELLED_GAP', 'EXECUTED')."""
    status_upper = new_status.upper()
    reference_iso = datetime.now(timezone.utc).isoformat()
    with get_sqlite_conn(db_path) as conn:
        if status_upper == "APPROVED":
            cur = conn.execute(
                """
                UPDATE pending_orders
                SET status = 'APPROVED'
                WHERE order_id = ?
                  AND status = 'PENDING'
                  AND expires_at IS NOT NULL
                  AND expires_at > ?;
                """,
                (order_id, reference_iso),
            )
        else:
            cur = conn.execute(
                "UPDATE pending_orders SET status = ? WHERE order_id = ?;",
                (status_upper, order_id),
            )
        if status_upper in {"REJECTED", "REJECTED_BY_USER", "CANCELLED_GAP", "EXPIRED"}:
            conn.execute(
                "UPDATE pending_orders SET reserved_cash_nok = 0.0 WHERE order_id = ?;",
                (order_id,),
            )
        return cur.rowcount > 0


def get_reserved_cash_nok(db_path: Path | str | None = None) -> float:
    """Return cash currently reserved by pending or approved BUY proposals."""
    init_sqlite_db(db_path)
    with get_sqlite_conn(db_path) as conn:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(reserved_cash_nok), 0.0)
            FROM pending_orders
            WHERE COALESCE(action, side, 'BUY') = 'BUY'
              AND status IN ('PENDING', 'APPROVED')
            """
        ).fetchone()
        return float(row[0] or 0.0)
