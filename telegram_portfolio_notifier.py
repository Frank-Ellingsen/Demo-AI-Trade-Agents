"""
Telegram Portfolio Status & Change Notifier (`telegram_portfolio_notifier.py`)
==============================================================================
1. `--daily`        : Sender daglig porteføljestatus (Equity, Cash, Realised/Unrealised P&L,
                      åpne posisjoner og aktive ordrer) fra Alpaca Paper til Telegram.
2. `--check-change` : Sammenligner gjeldende Alpaca Paper posisjoner/ordrer mot sist lagrede
                      tilstand (`data/portfolio_notify_state.json`) og sender Telegram-varsel
                      kun dersom en ordre er fylt/endret eller en posisjon er åpnet/lukket.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List

import requests
from dotenv import load_dotenv

from alpaca_execution import AlpacaClient

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "data" / "portfolio_notify_state.json"


def _send_telegram(text: str) -> Dict[str, Any]:
    load_dotenv(BASE_DIR / ".env", override=True)
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = str(os.getenv("TELEGRAM_CHAT_ID", "")).strip()
    if not token or not chat_id:
        raise ValueError("Mangler TELEGRAM_BOT_TOKEN eller TELEGRAM_CHAT_ID i .env")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(
        url,
        json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def format_portfolio_status_message(snap: Dict[str, Any], header: str = "📊 DAGLIG PORTEFØLJESTATUS (ALPACA PAPER)") -> str:
    now_ts = time.strftime("%Y-%m-%d %H:%M:%S")
    eq = float(snap.get("equity", 0.0))
    cash = float(snap.get("cash", 0.0))
    mv = float(snap.get("long_market_value", 0.0))
    today_pnl = float(snap.get("today_pnl", 0.0))
    today_pct = float(snap.get("today_pnl_pct", 0.0))
    real_pl = float(snap.get("realized_pl", 0.0))
    unreal_pl = float(snap.get("unrealized_pl", 0.0))
    positions = snap.get("positions", [])
    orders = snap.get("recent_orders", [])

    def _sgn(val: float) -> str:
        return f"+${val:,.2f}" if val >= 0 else f"-${abs(val):,.2f}"

    lines = [
        f"*{header}*",
        f"_Tidspunkt: {now_ts}_",
        "----------------------------------------",
        f"• *Total Egenkapital:* `${eq:,.2f}`",
        f"• *Tilgjengelig Cash:* `${cash:,.2f}`",
        f"• *Markedsverdi (Aksjer):* `${mv:,.2f}`",
        f"• *Dagens P&L:* `{_sgn(today_pnl)}` (`{today_pct:+.2f}%`)",
        f"• *Realised P&L:* `{_sgn(real_pl)}`",
        f"• *Urealisert P&L:* `{_sgn(unreal_pl)}`",
    ]

    if positions:
        lines.append("\n*Åpne Posisjoner:*")
        for p in positions:
            p_pl = float(p.get("unrealized_pl", 0.0))
            p_pct = float(p.get("unrealized_plpc", 0.0))
            lines.append(
                f"  - `{p['symbol']}`: {p['qty']} stk @ `${float(p['avg_entry_price']):,.2f}` "
                f"(MV `${float(p['market_value']):,.2f}` | P&L `{_sgn(p_pl)}` / `{p_pct:+.2f}%`)"
            )
    else:
        lines.append("\n*Åpne Posisjoner:* Ingen åpne posisjoner enda.")

    active_orders = [o for o in orders if o.get("status") in ("new", "accepted", "pending_new", "held", "partially_filled")]
    if active_orders:
        lines.append(f"\n*Aktive Ordrer ({len(active_orders)}):*")
        for o in active_orders[:8]:
            lim = f" @ `${float(o['limit_price']):,.2f}`" if o.get("limit_price") else ""
            lines.append(f"  - `{o['side']} {o['qty']} {o['symbol']}`{lim} ({o['status']})")

    return "\n".join(lines)


def _snapshot_fingerprint(snap: Dict[str, Any]) -> Dict[str, Any]:
    pos_sig = {
        p["symbol"]: {"qty": str(p.get("qty")), "avg_entry_price": round(float(p.get("avg_entry_price", 0)), 2)}
        for p in snap.get("positions", [])
    }
    ord_sig = {
        o["id"]: {"symbol": o.get("symbol"), "side": o.get("side"), "qty": str(o.get("qty")), "status": o.get("status")}
        for o in snap.get("recent_orders", [])[:20]
    }
    return {"positions": pos_sig, "orders": ord_sig}


def send_daily_status() -> Dict[str, Any]:
    load_dotenv(BASE_DIR / ".env", override=True)
    snap = AlpacaClient().get_portfolio_snapshot()
    msg = format_portfolio_status_message(snap)
    res = _send_telegram(msg)
    # Also refresh baseline state so immediate change-check doesn't duplicate
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(_snapshot_fingerprint(snap), indent=2), encoding="utf-8")
    return {"sent": bool(res.get("ok")), "message_id": res.get("result", {}).get("message_id")}


def check_and_notify_changes() -> Dict[str, Any]:
    load_dotenv(BASE_DIR / ".env", override=True)
    snap = AlpacaClient().get_portfolio_snapshot()
    curr = _snapshot_fingerprint(snap)

    prev = None
    if STATE_FILE.exists():
        try:
            prev = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            prev = None

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(curr, indent=2), encoding="utf-8")

    if prev is None:
        return {"changed": False, "reason": "Initialized baseline state"}

    prev_pos = prev.get("positions", {})
    curr_pos = curr.get("positions", {})

    opened = [sym for sym in curr_pos if sym not in prev_pos]
    closed = [sym for sym in prev_pos if sym not in curr_pos]
    resized = [
        sym for sym in curr_pos
        if sym in prev_pos and str(curr_pos[sym].get("qty")) != str(prev_pos[sym].get("qty"))
    ]

    if not opened and not closed and not resized:
        return {"changed": False}

    parts: List[str] = []
    if opened:
        parts.append("Åpnet: " + ", ".join(opened))
    if closed:
        parts.append("Lukket: " + ", ".join(closed))
    if resized:
        parts.append("Endret antall: " + ", ".join(resized))

    header = "🔔 POSISJONSENDRING (" + " | ".join(parts) + ")"
    msg = format_portfolio_status_message(snap, header=header)
    res = _send_telegram(msg)
    return {"changed": True, "opened": opened, "closed": closed, "resized": resized, "sent": bool(res.get("ok"))}


def main() -> None:
    parser = argparse.ArgumentParser(description="Telegram Portfolio Status & Change Notifier")
    parser.add_argument("--daily", action="store_true", help="Send daglig porteføljestatus til Telegram")
    parser.add_argument("--check-change", action="store_true", help="Varsle Telegram kun ved endring i posisjoner/ordrer")
    args = parser.parse_args()

    if args.check_change:
        res = check_and_notify_changes()
        print(json.dumps(res, ensure_ascii=False))
    else:
        res = send_daily_status()
        print(json.dumps(res, ensure_ascii=False))


if __name__ == "__main__":
    main()
