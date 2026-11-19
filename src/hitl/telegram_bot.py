"""Agent 4: Telegram Human-in-the-Loop (HITL) Bot Client.

Implements:
- Interactive BUY order proposal cards with inline buttons (`[✅ Godkjenn]` and `[❌ Avvis]`)
  and 14-hour auto-expiry metadata.
- Automated SELL / Stop-Loss execution notifications with an optional pre-open override button.
- Callback query processor (`process_telegram_callback`) that updates `pending_orders` in SQLite WAL.
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import Any

from config.settings import settings
from src.hitl.order_queue import update_order_status


def format_buy_proposal_message(
    order_id: str,
    ticker: str,
    strategy_name: str,
    shares: int,
    limit_price: float,
    stop_loss_price: float,
    llm_recommendation: str,
    risk_score: str,
    bull_case: str,
    devils_advocate: str,
    expires_at_iso: str,
) -> tuple[str, dict[str, Any]]:
    """Format a structured Telegram message and inline keyboard for a BUY order proposal."""
    notional_nok = shares * limit_price
    stop_dist_pct = ((stop_loss_price - limit_price) / limit_price) * 100.0 if limit_price > 0 else 0.0

    text = (
        f"📊 *OBX 15 KJØPSFORSLAG (HITL)*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"• *Ticker:* `{ticker}`\n"
        f"• *Strategi:* `{strategy_name}`\n"
        f"• *Antall:* `{shares}` aksjer (`{notional_nok:,.0f} NOK`)\n"
        f"• *Limit-kurs (Tick-avrundet):* `{limit_price:.2f} NOK`\n"
        f"• *ATR Stop-Loss:* `{stop_loss_price:.2f} NOK` (`{stop_dist_pct:.2f} %`)\n\n"
        f"🤖 *Agent 3 (AI Risk Officer):* `{llm_recommendation}` (Risiko: `{risk_score}`)\n"
        f"• *Bull Case:* {bull_case}\n"
        f"• *Djevelens Advokat:* {devils_advocate}\n\n"
        f"⏳ *Utløper automatisk (14t):* `{expires_at_iso[:16].replace('T', ' ')} UTC`"
    )

    reply_markup = {
        "inline_keyboard": [
            [
                {"text": "✅ Godkjenn (Send til Saxo)", "callback_data": f"APPROVE:{order_id}"},
                {"text": "❌ Avvis", "callback_data": f"REJECT:{order_id}"},
            ]
        ]
    }
    return text, reply_markup


def send_telegram_order_card(
    order_id: str,
    ticker: str,
    strategy_name: str,
    shares: int,
    limit_price: float,
    stop_loss_price: float,
    llm_recommendation: str,
    risk_score: str,
    bull_case: str,
    devils_advocate: str,
    expires_at_iso: str,
) -> dict[str, Any]:
    """Send interactive BUY proposal card to Telegram if credentials are configured; otherwise return dry-run payload."""
    text, reply_markup = format_buy_proposal_message(
        order_id=order_id,
        ticker=ticker,
        strategy_name=strategy_name,
        shares=shares,
        limit_price=limit_price,
        stop_loss_price=stop_loss_price,
        llm_recommendation=llm_recommendation,
        risk_score=risk_score,
        bull_case=bull_case,
        devils_advocate=devils_advocate,
        expires_at_iso=expires_at_iso,
    )

    if not settings.telegram_bot_token or settings.telegram_bot_token.startswith("your_"):
        return {"sent": False, "mode": "dry_run", "text": text, "reply_markup": reply_markup}

    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    payload = json.dumps(
        {
            "chat_id": settings.telegram_chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "reply_markup": reply_markup,
        }
    ).encode("utf-8")

    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10.0) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        return {"sent": True, "mode": "live", "response": data}


def process_telegram_callback(callback_data: str, db_path: Path | None = None) -> tuple[str, str]:
    """Process inline button callback (`APPROVE:<order_id>` or `REJECT:<order_id>`) in SQLite WAL."""
    if ":" not in callback_data:
        raise ValueError("Invalid callback_data format. Expected 'ACTION:order_id'.")

    action_prefix, order_id = callback_data.split(":", 1)
    action_upper = action_prefix.upper()

    if action_upper == "APPROVE":
        new_status = "APPROVED"
    elif action_upper == "REJECT":
        new_status = "REJECTED"
    else:
        raise ValueError(f"Unsupported callback action: {action_upper}")

    updated = update_order_status(order_id=order_id, new_status=new_status, db_path=db_path)
    if not updated:
        raise KeyError(f"Order ID not found: {order_id}")
    return order_id, new_status
