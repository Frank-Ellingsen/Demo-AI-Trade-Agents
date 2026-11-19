"""Telegram Human-In-The-Loop (HITL) Bot for OBX Trading System (`telegram_hitl_bot.py`).

Allows the user to approve or reject trade signals directly from Telegram with lock-free
writing to SQLite (`data/hitl_orders.db` in WAL mode). Works both with `python-telegram-bot`
installed and in zero-dependency mode for local testing and HTTP webhook callbacks.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from config.settings import settings
from src.db.sqlite_store import init_sqlite_db
from src.hitl.order_queue import enqueue_order_proposal, update_order_status as _core_update_status
from src.hitl.telegram_bot import process_telegram_callback, send_telegram_order_card

try:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
    from telegram.ext import Application, ContextTypes

    HAS_PTB = True
except ImportError:
    InlineKeyboardButton = None
    InlineKeyboardMarkup = None
    Update = Any
    Application = Any
    ContextTypes = Any
    HAS_PTB = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("TelegramHITL")

DB_PATH = os.environ.get("HITL_DB_PATH", str(settings.sqlite_db_path))
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", settings.telegram_bot_token or "MOCK_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", settings.telegram_chat_id or "MOCK_CHAT_ID")


def init_db(db_path: str | Path = DB_PATH) -> Path:
    """Initialize the SQLite WAL order queue table."""
    return init_sqlite_db(db_path)


def log_order_proposal(
    order_id: str,
    ticker: str,
    side: str,
    amount: int,
    limit_price: float,
    reason: str,
    db_path: str | Path = DB_PATH,
    stop_loss_price: float | None = None,
) -> dict[str, Any]:
    """Register a new trade signal with PENDING status (14h auto-expiry) in SQLite WAL."""
    effective_stop = (
        round(float(stop_loss_price), 2)
        if stop_loss_price is not None
        else round(float(limit_price) * 0.95, 2)
    )
    return enqueue_order_proposal(
        order_id=order_id,
        ticker=ticker,
        action=side.upper(),
        shares=int(amount),
        signal_close_price=float(limit_price),
        limit_price=float(limit_price),
        stop_loss_price=effective_stop,
        strategy_name="rsi_mean_reversion",
        bull_case=reason,
        devils_advocate="",
        db_path=db_path,
    )


def update_order_status(order_id: str, new_status: str, db_path: str | Path = DB_PATH) -> bool:
    """Update order status after user interaction."""
    return _core_update_status(order_id=order_id, new_status=new_status, db_path=db_path)


async def send_trade_proposal(
    app: Any,
    order_id: str,
    ticker: str,
    side: str,
    amount: int,
    limit_price: float,
    reason: str,
    chat_id: str = CHAT_ID,
    db_path: str | Path = DB_PATH,
) -> dict[str, Any]:
    """Send an interactive decision card to Telegram and log to SQLite WAL."""
    queued = log_order_proposal(order_id, ticker, side, amount, limit_price, reason, db_path=db_path)
    total_value = amount * limit_price

    text = (
        f"🚨 <b>NYTT SIGNAL FOR GODKJENNING (HITL)</b> 🚨\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Aksje:</b> {ticker}\n"
        f"• <b>Handling:</b> {side} Limit\n"
        f"• <b>Volum:</b> {amount} stk (~{total_value:,.0f} NOK)\n"
        f"• <b>Limit-kurs:</b> {limit_price:.2f} NOK (MiFID II avrundet)\n"
        f"• <b>AI-begrunnelse:</b> {reason}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>Utløper automatisk om 14 timer hvis ubesvart.</i>"
    )

    if HAS_PTB and app is not None and hasattr(app, "bot"):
        keyboard = [
            [
                InlineKeyboardButton("✅ Godkjenn Limit-ordre", callback_data=f"APPROVE:{order_id}"),
                InlineKeyboardButton("❌ Avvis", callback_data=f"REJECT:{order_id}"),
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await app.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=reply_markup)
        return {"sent": True, "order_id": order_id}

    return send_telegram_order_card(
        order_id=order_id,
        ticker=ticker,
        strategy_name="rsi_mean_reversion",
        shares=amount,
        limit_price=limit_price,
        stop_loss_price=round(limit_price * 0.95, 2),
        llm_recommendation="GO",
        risk_score="LOW",
        bull_case=reason,
        devils_advocate="Se NewsWeb",
        expires_at_iso=queued["expires_at"],
    )


async def handle_callback(update: Any, context: Any = None, db_path: str | Path = DB_PATH) -> str:
    """Handle user click on `[Godkjenn]` or `[Avvis]` inline buttons."""
    query = update.callback_query
    await query.answer()

    action, order_id = query.data.split(":", 1)
    if action.upper() == "APPROVE":
        update_order_status(order_id, "APPROVED", db_path=db_path)
        new_text = query.message.text + "\n\n🟢 <b>STATUS: GODKJENT AV BRUKER</b>\n<i>Klar for eksekvering via Saxo OpenAPI.</i>"
    else:
        update_order_status(order_id, "REJECTED_BY_USER", db_path=db_path)
        new_text = query.message.text + "\n\n🔴 <b>STATUS: AVVIST AV BRUKER</b>"

    await query.edit_message_text(text=new_text, parse_mode="HTML")
    return order_id


__all__ = [
    "init_db",
    "log_order_proposal",
    "update_order_status",
    "send_trade_proposal",
    "handle_callback",
    "process_telegram_callback",
]
