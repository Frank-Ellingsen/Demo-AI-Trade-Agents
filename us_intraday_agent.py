#!/usr/bin/env python3
"""
us_intraday_agent.py
=============================================================================
Autonomous US Intraday Trading Agent Specialist (Alpaca Paper Only)
=============================================================================
Skreddersydd autonom intradag-spesialist for likvide amerikanske aksjer
(SPY, QQQ, NVDA, AAPL, MSFT, META, AMZN, TSLA, AMD, AVGO) med integrasjon
KUN mot Alpaca Paper REST API (`https://paper-api.alpaca.markets/v2`) og
sanntids Telegram-rapportering av alle handler og realisert intradag P/L.

KJERNEKRAV OG RISIKOREGLER:
1. Kun Alpaca Paper: Hardkodet sikkerhetssperre mot live meglerkontoer.
2. Posisjonsstørrelse: Maks 10 % av samlet porteføljeverdi (kun innenfor ledig cash).
3. Hurdle Rate & Friksjon: Forventet netto gevinst MÅ overstige SEC Section 31,
   FINRA TAF, spread/slippage og minimumskrav (0.40 % netto & R/R >= 1.8x).
4. Intradag-strategi:
   - 15-min Opening Range Breakout (ORB_BREAKOUT_15M)
   - VWAP Pullback med volumbekreftelse (VWAP_PULLBACK)
5. Autonom eksekvering + Telegram-rapportering:
   - Handler automatisk med Limit OCO Bracket (DAY) på Alpaca Paper.
   - Sender umiddelbar Telegram-rapport ved hver åpnet og lukket handel +
     løpende realisert intradag P/L.
6. Obligatorisk EOD-flatting kl. 15:55 EST (21:55 CET) for å eliminere
   overnight gap-risiko.
=============================================================================
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("USIntradayAgent")

BASE_DIR = Path(__file__).resolve().parent
INTRADAY_DB_PATH = BASE_DIR / "data" / "intraday_agent.db"

# Megler- og regulatoriske gebyrsatser for US Equities (Alpaca Paper)
SEC_FEE_RATE = 0.0000278          # SEC Section 31 transaksjonsgebyr (ved salg)
FINRA_TAF_PER_SHARE = 0.000166    # FINRA Trading Activity Fee (maks $8.30 per handel)
MAX_FINRA_TAF = 8.30
ESTIMATED_HALF_SPREAD_BPS = 2.0   # 2 bps (0.02%) for likvide US megacaps
EFFECTIVE_TAX_RATE = 0.3784       # Norsk gevinstbeskatning aksjer utenfor ASK (37.84%)
MIN_NET_PROFIT_HURDLE_PCT = 0.40  # Minstekrav til netto gevinstpotensial etter friksjon
MIN_RISK_REWARD_RATIO = 1.80

INTRADAY_UNIVERSE = ["NVDA", "AAPL", "MSFT", "META", "AMZN", "TSLA", "SPY", "QQQ", "AMD", "AVGO"]
NY_TZ = ZoneInfo("America/New_York")


@dataclass
class IntradaySignal:
    ticker: str
    strategy: str               # 'ORB_BREAKOUT_15M' eller 'VWAP_PULLBACK'
    direction: str              # 'BUY'
    current_price: float
    stop_loss: float
    take_profit: float
    volume_ratio_to_avg: float
    vwap: float


def _init_intraday_db() -> None:
    INTRADAY_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(INTRADAY_DB_PATH) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS intraday_trades (
                trade_id TEXT PRIMARY KEY,
                trade_date TEXT,
                opened_at TEXT,
                closed_at TEXT,
                ticker TEXT,
                strategy TEXT,
                side TEXT,
                shares INTEGER,
                entry_price REAL,
                stop_loss REAL,
                take_profit REAL,
                exit_price REAL,
                realized_pnl_usd REAL DEFAULT 0.0,
                realized_pnl_pct REAL DEFAULT 0.0,
                friction_usd REAL DEFAULT 0.0,
                after_tax_pnl_usd REAL DEFAULT 0.0,
                alpaca_order_id TEXT,
                status TEXT,
                telegram_notified INTEGER DEFAULT 0
            )"""
        )
        conn.commit()


def send_telegram_intraday(message: str) -> bool:
    """Send Markdown/HTML status or realized P/L alert directly to Telegram."""
    load_dotenv(override=True)
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat_id:
        logger.warning("Telegram ikke konfigurert (mangler TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID).")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status == 200
    except Exception as exc:
        logger.warning(f"Telegram-sending feilet: {exc}")
        return False


class USIntradaySpecialist:
    """Autonomous US Intraday Agent — strictly Alpaca Paper Only."""

    def __init__(self, max_portfolio_pct: float = 0.10):
        self.max_portfolio_pct = max(0.01, min(0.25, float(max_portfolio_pct)))
        self.alpaca_key = os.getenv("ALPACA_API_KEY") or os.getenv("alpaca_key") or ""
        self.alpaca_secret = os.getenv("ALPACA_SECRET_KEY") or os.getenv("alpaca_secret") or ""
        self.base_url = "https://paper-api.alpaca.markets/v2"
        _init_intraday_db()

    def evaluate_capital_and_sizing(self, portfolio_value_usd: float, cash_usd: float, price_usd: float) -> Dict[str, Any]:
        """Krav 1: Maks 10 % av porteføljeverdien og kun innenfor ledig kontantbeholdning."""
        max_allowed_allocation = portfolio_value_usd * self.max_portfolio_pct
        effective_budget = min(max_allowed_allocation, max(0.0, cash_usd))

        if price_usd <= 0 or effective_budget < price_usd:
            return {
                "eligible": False,
                "reason": f"Utilstrekkelig ledig kapital (Budsjett: ${effective_budget:.2f}, Kurs: ${price_usd:.2f})",
                "shares": 0,
                "order_value_usd": 0.0,
                "max_allowed_usd": round(max_allowed_allocation, 2),
                "effective_budget_usd": round(effective_budget, 2),
            }

        shares = int(effective_budget // price_usd)
        order_value = shares * price_usd
        return {
            "eligible": True,
            "shares": shares,
            "order_value_usd": round(order_value, 2),
            "pct_of_portfolio": round((order_value / portfolio_value_usd) * 100.0, 2) if portfolio_value_usd > 0 else 0.0,
            "cash_remaining_usd": round(cash_usd - order_value, 2),
            "max_allowed_usd": round(max_allowed_allocation, 2),
            "effective_budget_usd": round(effective_budget, 2),
        }

    def calculate_all_in_friction(self, shares: int, entry_price: float, exit_price: float) -> Dict[str, float]:
        """Beregner SEC, FINRA TAF, spread og effektiv transaksjonsfriksjon (Alpaca $0 kurtasje)."""
        buy_notional = shares * entry_price
        sell_notional = shares * exit_price
        commission = 0.0
        sec_fee = sell_notional * SEC_FEE_RATE
        finra_taf = min(shares * FINRA_TAF_PER_SHARE, MAX_FINRA_TAF)
        regulatory_fees = sec_fee + finra_taf
        spread_cost = (buy_notional + sell_notional) * (ESTIMATED_HALF_SPREAD_BPS / 10000.0)
        total_friction = commission + regulatory_fees + spread_cost
        friction_pct = (total_friction / buy_notional) * 100.0 if buy_notional > 0 else 0.0
        return {
            "commission_usd": commission,
            "sec_fee_usd": round(sec_fee, 4),
            "finra_taf_usd": round(finra_taf, 4),
            "spread_slippage_usd": round(spread_cost, 2),
            "total_friction_usd": round(total_friction, 2),
            "friction_pct": round(friction_pct, 4),
        }

    def evaluate_hurdle_rate(self, signal: IntradaySignal, shares: int) -> Dict[str, Any]:
        """Krav 2: Gevinstrealisering må overstige friksjon, skatt og minimums hurdle rate."""
        gross_gain_usd = (signal.take_profit - signal.current_price) * shares
        gross_loss_usd = (signal.current_price - signal.stop_loss) * shares
        gross_return_pct = ((signal.take_profit - signal.current_price) / signal.current_price) * 100.0 if signal.current_price > 0 else 0.0
        stop_risk_pct = ((signal.current_price - signal.stop_loss) / signal.current_price) * 100.0 if signal.current_price > 0 else 0.0

        friction = self.calculate_all_in_friction(shares, signal.current_price, signal.take_profit)
        net_profit_usd = gross_gain_usd - friction["total_friction_usd"]
        invested = shares * signal.current_price
        net_profit_pct = (net_profit_usd / invested) * 100.0 if invested > 0 else 0.0
        risk_reward_ratio = net_profit_usd / gross_loss_usd if gross_loss_usd > 0 else 0.0
        tax_cost_est_usd = max(0.0, net_profit_usd * EFFECTIVE_TAX_RATE)
        after_tax_gain_usd = net_profit_usd - tax_cost_est_usd
        passes_hurdle = (net_profit_pct >= MIN_NET_PROFIT_HURDLE_PCT) and (risk_reward_ratio >= MIN_RISK_REWARD_RATIO)

        return {
            "passes_hurdle": passes_hurdle,
            "gross_return_pct": round(gross_return_pct, 2),
            "stop_risk_pct": round(stop_risk_pct, 2),
            "net_profit_pct": round(net_profit_pct, 2),
            "risk_reward_ratio": round(risk_reward_ratio, 2),
            "gross_gain_usd": round(gross_gain_usd, 2),
            "net_profit_usd": round(net_profit_usd, 2),
            "tax_cost_est_usd": round(tax_cost_est_usd, 2),
            "after_tax_gain_usd": round(after_tax_gain_usd, 2),
            "total_friction_usd": friction["total_friction_usd"],
            "friction_pct": friction["friction_pct"],
            "sec_fee_usd": friction["sec_fee_usd"],
            "finra_taf_usd": friction["finra_taf_usd"],
            "spread_slippage_usd": friction["spread_slippage_usd"],
        }

    def check_eod_time_window(self) -> Dict[str, Any]:
        """Reell US-børstid (America/New_York): 09:30–16:00 ET, cut-off 15:30 ET, tvangslukking 15:55 ET."""
        now_ny = datetime.now(NY_TZ)
        is_weekday = now_ny.weekday() < 5
        t_now = now_ny.time()
        market_open = is_weekday and (dtime(9, 30) <= t_now < dtime(16, 0))
        allow_new_entries = is_weekday and (dtime(9, 45) <= t_now < dtime(15, 30))
        force_eod_exit = is_weekday and (dtime(15, 55) <= t_now < dtime(16, 5))

        eod_target = now_ny.replace(hour=15, minute=55, second=0, microsecond=0)
        secs_to_eod = max(0, int((eod_target - now_ny).total_seconds())) if market_open else 0
        hrs, rem = divmod(secs_to_eod, 3600)
        mins, secs = divmod(rem, 60)

        return {
            "ny_time": now_ny.strftime("%H:%M:%S EST"),
            "is_weekday": is_weekday,
            "market_open": market_open,
            "allow_new_entries": allow_new_entries,
            "force_eod_exit": force_eod_exit,
            "seconds_to_eod_flatten": secs_to_eod,
            "countdown_str": f"{hrs:02d}:{mins:02d}:{secs:02d}" if market_open else "US Børs Stengt",
            "eod_deadline_est": "15:55 EST (21:55 CET)",
        }

    def generate_intraday_decision(
        self,
        signal: IntradaySignal,
        portfolio_value_usd: float,
        cash_usd: float,
    ) -> Dict[str, Any]:
        """Full samlet evaluering av et intradag-kandidatsignal."""
        sizing = self.evaluate_capital_and_sizing(portfolio_value_usd, cash_usd, signal.current_price)
        if not sizing["eligible"]:
            return {
                "decision": "VETO_CAPITAL_CONSTRAINED",
                "ticker": signal.ticker,
                "reason": sizing["reason"],
                "sizing": sizing,
            }

        hurdle = self.evaluate_hurdle_rate(signal, sizing["shares"])
        if not hurdle["passes_hurdle"]:
            return {
                "decision": "VETO_HURDLE_RATE_NOT_MET",
                "ticker": signal.ticker,
                "reason": (
                    f"Netto avkastning ({hurdle['net_profit_pct']}%) eller R/R-forhold "
                    f"({hurdle['risk_reward_ratio']}x) overstiger ikke gebyrer/friksjon og kravet "
                    f"(>= {MIN_NET_PROFIT_HURDLE_PCT}% netto & >= {MIN_RISK_REWARD_RATIO}x R/R)."
                ),
                "sizing": sizing,
                "hurdle": hurdle,
            }

        return {
            "decision": "APPROVED_FOR_EXECUTION",
            "ticker": signal.ticker,
            "strategy": signal.strategy,
            "shares": sizing["shares"],
            "order_value_usd": sizing["order_value_usd"],
            "pct_of_portfolio": sizing["pct_of_portfolio"],
            "entry_price": round(signal.current_price, 2),
            "stop_loss": round(signal.stop_loss, 2),
            "take_profit": round(signal.take_profit, 2),
            "risk_reward_ratio": hurdle["risk_reward_ratio"],
            "net_profit_est_usd": hurdle["net_profit_usd"],
            "after_tax_gain_usd": hurdle["after_tax_gain_usd"],
            "tax_cost_est_usd": hurdle["tax_cost_est_usd"],
            "friction_usd": hurdle["total_friction_usd"],
            "hurdle": hurdle,
            "sizing": sizing,
            "order_type": "LIMIT_OCO_BRACKET",
            "time_in_force": "DAY",
            "auto_eod_flatten": True,
        }

    def scan_live_intraday_candidates(self, tickers: Optional[List[str]] = None) -> List[IntradaySignal]:
        """Scan 5-min intraday bars from Yahoo Finance for 15-min ORB Breakout & VWAP Pullback."""
        import requests

        hdr = {"User-Agent": "Mozilla/5.0"}
        candidates: List[IntradaySignal] = []
        for sym in (tickers or INTRADAY_UNIVERSE):
            try:
                r = requests.get(
                    f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
                    params={"range": "1d", "interval": "5m"},
                    headers=hdr,
                    timeout=5,
                )
                if r.status_code != 200:
                    continue
                res0 = (r.json().get("chart") or {}).get("result") or []
                if not res0:
                    continue
                q = res0[0].get("indicators", {}).get("quote", [{}])[0]
                highs = [float(x) for x in (q.get("high") or []) if x is not None]
                lows = [float(x) for x in (q.get("low") or []) if x is not None]
                closes = [float(x) for x in (q.get("close") or []) if x is not None]
                vols = [float(x or 0) for x in (q.get("volume") or [])]
                if len(closes) < 4:
                    continue

                price = closes[-1]
                # 15-min opening range = first three 5m candles
                orb_high = max(highs[:3])
                orb_low = min(lows[:3])
                orb_range = max(orb_high - orb_low, price * 0.004)

                # Intraday VWAP
                n = min(len(closes), len(vols))
                tot_v = sum(vols[:n])
                vwap = sum(closes[i] * vols[i] for i in range(n)) / tot_v if tot_v > 0 else price
                avg_v = (tot_v / n) if n > 0 else 1.0
                vol_ratio = (vols[n - 1] / avg_v) if avg_v > 0 else 1.0

                # Strategy 1: 15-min ORB Breakout above orb_high & VWAP
                if price > orb_high and price > vwap and vol_ratio >= 1.15:
                    sl = round(max(orb_low, price - orb_range * 0.65), 2)
                    risk = max(price - sl, price * 0.004)
                    tp = round(price + risk * 2.25, 2)
                    candidates.append(
                        IntradaySignal(sym, "ORB_BREAKOUT_15M", "BUY", round(price, 2), sl, tp, round(vol_ratio, 2), round(vwap, 2))
                    )
                # Strategy 2: VWAP Pullback near VWAP (+0.0% to +0.35% above VWAP)
                elif vwap <= price <= vwap * 1.0035 and vol_ratio >= 1.10:
                    sl = round(vwap * 0.993, 2)
                    risk = max(price - sl, price * 0.004)
                    tp = round(price + risk * 2.20, 2)
                    candidates.append(
                        IntradaySignal(sym, "VWAP_PULLBACK", "BUY", round(price, 2), sl, tp, round(vol_ratio, 2), round(vwap, 2))
                    )
            except Exception:
                continue
        return candidates

    def execute_autonomous_trade(self, decision: Dict[str, Any], notify_telegram: bool = True) -> Dict[str, Any]:
        """Autonom eksekvering av godkjent intradag-ordre KUN på Alpaca Paper + Telegram-rapport."""
        if decision.get("decision") != "APPROVED_FOR_EXECUTION":
            return {"success": False, "error": decision.get("reason", "Ikke godkjent")}

        from alpaca_execution import AlpacaClient

        client = AlpacaClient()
        sym = str(decision["ticker"]).upper()
        shares = int(decision["shares"])
        entry = float(decision["entry_price"])
        sl = float(decision["stop_loss"])
        tp = float(decision["take_profit"])
        tid = f"ID-{uuid.uuid4().hex[:6].upper()}"

        order = client.place_bracket_order(
            symbol=sym,
            qty=shares,
            side="buy",
            limit_price=entry,
            take_profit_price=tp,
            stop_loss_price=sl,
            time_in_force="day",
            client_order_id=f"intraday_{tid}",
        )
        alp_oid = str(order.get("id") or "")
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        today_str = datetime.now(NY_TZ).date().isoformat()

        with sqlite3.connect(INTRADAY_DB_PATH) as conn:
            conn.execute(
                """INSERT INTO intraday_trades
                (trade_id, trade_date, opened_at, ticker, strategy, side, shares, entry_price,
                 stop_loss, take_profit, friction_usd, alpaca_order_id, status, telegram_notified)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    tid, today_str, now_str, sym, str(decision.get("strategy") or "ORB_BREAKOUT_15M"),
                    "BUY", shares, entry, sl, tp, float(decision.get("friction_usd") or 0.0),
                    alp_oid, "OPEN", 0,
                ),
            )
            conn.commit()

        tg_sent = False
        if notify_telegram:
            msg = (
                f"⚡ <b>AI INTRADAY AGENT — AUTONOM HANDEL ÅPNET (Alpaca Paper)</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"• <b>Ticker:</b> <code>{sym}</code> ({decision.get('strategy')})\n"
                f"• <b>Ordre:</b> BUY {shares} stk @ <b>${entry:.2f}</b> (${decision['order_value_usd']:,.2f} · {decision['pct_of_portfolio']:.1f}%)\n"
                f"• <b>OCO Bracket (DAY):</b> SL <code>${sl:.2f}</code> | TP <code>${tp:.2f}</code> (R/R {decision['risk_reward_ratio']}x)\n"
                f"• <b>Est. Netto / e. skatt:</b> +${decision['net_profit_est_usd']:.2f} / +${decision['after_tax_gain_usd']:.2f}\n"
                f"• <b>Auto EOD-flatting:</b> 15:55 EST (21:55 CET)\n"
                f"• <b>Ref:</b> <code>{tid}</code>"
            )
            tg_sent = send_telegram_intraday(msg)
            if tg_sent:
                with sqlite3.connect(INTRADAY_DB_PATH) as conn:
                    conn.execute("UPDATE intraday_trades SET telegram_notified=1 WHERE trade_id=?", (tid,))
                    conn.commit()

        return {"success": True, "trade_id": tid, "alpaca_order_id": alp_oid, "telegram_sent": tg_sent, "decision": decision}

    def sync_closed_trades_and_report(self) -> Dict[str, Any]:
        """Sjekker åpne intradag-handler mot Alpaca Paper, beregner realisert P/L og sender Telegram ved lukking."""
        from alpaca_execution import AlpacaClient

        client = AlpacaClient()
        snap = client.get_portfolio_snapshot()
        open_syms = {str(p.get("symbol") or "").upper(): p for p in (snap.get("positions") or [])}
        recent_orders = snap.get("recent_orders") or []
        today_str = datetime.now(NY_TZ).date().isoformat()
        closed_now = 0

        with sqlite3.connect(INTRADAY_DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            open_rows = conn.execute(
                "SELECT * FROM intraday_trades WHERE status='OPEN' AND trade_date=?",
                (today_str,),
            ).fetchall()

            for row in open_rows:
                sym = str(row["ticker"]).upper()
                if sym in open_syms:
                    continue
                # Find filled sell exit price if closed via TP/SL/EOD
                sell_fill = next(
                    (
                        o for o in recent_orders
                        if str(o.get("symbol") or "").upper() == sym
                        and str(o.get("side") or "").upper() == "SELL"
                        and str(o.get("status") or "").lower() == "filled"
                        and o.get("filled_avg_price")
                    ),
                    None,
                )
                if not sell_fill:
                    continue

                exit_px = float(sell_fill["filled_avg_price"])
                entry_px = float(row["entry_price"])
                shares = int(row["shares"])
                fric = self.calculate_all_in_friction(shares, entry_px, exit_px)["total_friction_usd"]
                real_pnl = (exit_px - entry_px) * shares - fric
                real_pct = ((exit_px - entry_px) / entry_px * 100.0) if entry_px > 0 else 0.0
                after_tax = real_pnl - max(0.0, real_pnl * EFFECTIVE_TAX_RATE)
                closed_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

                conn.execute(
                    """UPDATE intraday_trades
                    SET status='CLOSED', closed_at=?, exit_price=?, realized_pnl_usd=?,
                        realized_pnl_pct=?, friction_usd=?, after_tax_pnl_usd=?
                    WHERE trade_id=?""",
                    (closed_at, round(exit_px, 2), round(real_pnl, 2), round(real_pct, 2), round(fric, 2), round(after_tax, 2), row["trade_id"]),
                )
                conn.commit()
                closed_now += 1

                cum_pnl = conn.execute(
                    "SELECT COALESCE(SUM(realized_pnl_usd), 0.0) FROM intraday_trades WHERE trade_date=? AND status='CLOSED'",
                    (today_str,),
                ).fetchone()[0]

                emoji = "🟢" if real_pnl >= 0 else "🔴"
                msg = (
                    f"{emoji} <b>AI INTRADAY — HANDEL LUKKET &amp; REALISERT P/L</b>\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"• <b>Ticker:</b> <code>{sym}</code> ({shares} stk)\n"
                    f"• <b>Entry → Exit:</b> ${entry_px:.2f} → <b>${exit_px:.2f}</b> ({real_pct:+.2f}%)\n"
                    f"• <b>Realisert P/L (netto):</b> <b>{real_pnl:+,.2f} USD</b> (e. 37.84% skatt: {after_tax:+,.2f} USD)\n"
                    f"• <b>Akkumulert Intradag P/L i dag:</b> <b>{float(cum_pnl):+,.2f} USD</b>"
                )
                send_telegram_intraday(msg)

        return {"closed_now": closed_now}

    def flatten_intraday_positions(self, reason: str = "EOD 15:55 EST Auto-Flatten") -> Dict[str, Any]:
        """Lukker alle åpne intradag-posisjoner på Alpaca Paper og sender realisert P/L-rapport på Telegram."""
        import requests
        from alpaca_execution import AlpacaClient

        client = AlpacaClient()
        today_str = datetime.now(NY_TZ).date().isoformat()
        closed_syms: List[str] = []

        with sqlite3.connect(INTRADAY_DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM intraday_trades WHERE status='OPEN' AND trade_date=?",
                (today_str,),
            ).fetchall()
            for r in rows:
                sym = str(r["ticker"]).upper()
                try:
                    resp = requests.delete(f"{client.endpoint}/positions/{sym}", headers=client.headers, timeout=10)
                    if resp.status_code in (200, 204):
                        closed_syms.append(sym)
                except Exception:
                    pass

        time.sleep(1.0)
        self.sync_closed_trades_and_report()
        rep = self.send_intraday_pnl_telegram_summary(header=f"🏁 EOD INTRADAG FLATTING ({reason})")
        return {"success": True, "flattened_symbols": closed_syms, "telegram_sent": rep.get("telegram_sent", False)}

    def get_today_summary(self) -> Dict[str, Any]:
        _init_intraday_db()
        today_str = datetime.now(NY_TZ).date().isoformat()
        with sqlite3.connect(INTRADAY_DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            trades = [dict(r) for r in conn.execute("SELECT * FROM intraday_trades ORDER BY opened_at DESC LIMIT 30").fetchall()]
            today_rows = [t for t in trades if t.get("trade_date") == today_str]
        realized_today = sum(float(t.get("realized_pnl_usd") or 0.0) for t in today_rows if t.get("status") == "CLOSED")
        after_tax_today = sum(float(t.get("after_tax_pnl_usd") or 0.0) for t in today_rows if t.get("status") == "CLOSED")
        open_today = sum(1 for t in today_rows if t.get("status") == "OPEN")
        closed_today = sum(1 for t in today_rows if t.get("status") == "CLOSED")
        return {
            "trade_date": today_str,
            "open_count": open_today,
            "closed_count": closed_today,
            "realized_pnl_today_usd": round(realized_today, 2),
            "after_tax_today_usd": round(after_tax_today, 2),
            "trades": trades,
        }

    def send_intraday_pnl_telegram_summary(self, header: str = "📊 AI INTRADAY P/L RAPPORT (Alpaca Paper)") -> Dict[str, Any]:
        from alpaca_execution import AlpacaClient

        summary = self.get_today_summary()
        try:
            alp = AlpacaClient().get_portfolio_snapshot()
        except Exception:
            alp = {"equity": 100000.0, "cash": 100000.0, "today_pnl": 0.0, "today_pnl_pct": 0.0}

        msg = (
            f"<b>{header}</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"• <b>Dato:</b> {summary['trade_date']}\n"
            f"• <b>Intradag-handler i dag:</b> {summary['open_count']} åpne · {summary['closed_count']} lukkede\n"
            f"• <b>Realisert Intradag P/L:</b> <b>{summary['realized_pnl_today_usd']:+,.2f} USD</b> (e. skatt: {summary['after_tax_today_usd']:+,.2f} USD)\n"
            f"• <b>Alpaca Paper Egenkapital:</b> ${float(alp.get('equity', 0)):,.2f} (I dag: {float(alp.get('today_pnl', 0)):+,.2f} USD / {float(alp.get('today_pnl_pct', 0)):+.2f}%)\n"
            f"• <b>Ledig Cash:</b> ${float(alp.get('cash', 0)):,.2f}"
        )
        sent = send_telegram_intraday(msg)
        return {"success": True, "telegram_sent": sent, "summary": summary}


class AutonomousIntradayRunner:
    """Bakgrunnstråd som kjører AI Intraday Agent autonomt på Alpaca Paper og rapporterer på Telegram."""

    def __init__(self) -> None:
        self.specialist = USIntradaySpecialist(max_portfolio_pct=0.10)
        self.running = False
        self.interval_sec = 180
        self.last_scan_at = ""
        self.last_candidates: List[Dict[str, Any]] = []
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._eod_flattened_date = ""

    def start(self, max_portfolio_pct: float = 0.10, interval_sec: int = 180) -> Dict[str, Any]:
        with self._lock:
            self.specialist.max_portfolio_pct = max(0.01, min(0.25, float(max_portfolio_pct)))
            self.interval_sec = max(30, min(900, int(interval_sec)))
            if not self.running:
                self.running = True
                self._thread = threading.Thread(target=self._loop, daemon=True)
                self._thread.start()
                send_telegram_intraday(
                    f"🤖 <b>AI INTRADAY AGENT AKTIVERT (Autonom Alpaca Paper)</b>\n"
                    f"• Maks allokering per handel: <b>{self.specialist.max_portfolio_pct * 100:.0f}%</b> (kun ledig cash)\n"
                    f"• Strategier: 15-min ORB Breakout &amp; VWAP Pullback\n"
                    f"• EOD Auto-Flatting: <b>15:55 EST (21:55 CET)</b>"
                )
        return self.get_status()

    def stop(self) -> Dict[str, Any]:
        with self._lock:
            was_running = self.running
            self.running = False
        if was_running:
            send_telegram_intraday("⏸️ <b>AI INTRADAY AGENT SATT PÅ PAUSE</b> (Ingen nye autonome intradag-ordrer).")
        return self.get_status()

    def run_single_cycle(self, execute_approved: bool = False) -> Dict[str, Any]:
        from alpaca_execution import AlpacaClient

        win = self.specialist.check_eod_time_window()
        today_str = datetime.now(NY_TZ).date().isoformat()
        try:
            alp = AlpacaClient().get_portfolio_snapshot()
        except Exception as exc:
            return {"success": False, "error": str(exc), "window": win}

        # Sync any closed trades & send realized P/L to Telegram
        self.specialist.sync_closed_trades_and_report()

        # Mandatory EOD Flatten at 15:55 EST (21:55 CET)
        if execute_approved and win["force_eod_exit"] and self._eod_flattened_date != today_str:
            self._eod_flattened_date = today_str
            self.specialist.flatten_intraday_positions()

        eq = float(alp.get("equity") or 100000.0)
        cash = float(alp.get("cash") or 0.0)
        open_syms = {str(p.get("symbol") or "").upper() for p in (alp.get("positions") or [])}

        signals = self.specialist.scan_live_intraday_candidates()
        evaluated: List[Dict[str, Any]] = []
        executed: List[Dict[str, Any]] = []

        for sig in signals:
            dec = self.specialist.generate_intraday_decision(sig, eq, cash)
            dec["vwap"] = sig.vwap
            dec["volume_ratio"] = sig.volume_ratio_to_avg
            evaluated.append(dec)

            if (
                execute_approved
                and win["allow_new_entries"]
                and dec.get("decision") == "APPROVED_FOR_EXECUTION"
                and sig.ticker not in open_syms
            ):
                try:
                    ex = self.specialist.execute_autonomous_trade(dec, notify_telegram=True)
                    if ex.get("success"):
                        executed.append(ex)
                        open_syms.add(sig.ticker)
                        cash = max(0.0, cash - float(dec.get("order_value_usd") or 0.0))
                except Exception as exc:
                    logger.warning(f"Autonom eksekvering feilet for {sig.ticker}: {exc}")

        self.last_scan_at = time.strftime("%H:%M:%S")
        self.last_candidates = evaluated
        return {
            "success": True,
            "scanned_at": self.last_scan_at,
            "window": win,
            "candidates": evaluated,
            "executed": executed,
        }

    def _loop(self) -> None:
        while self.running:
            try:
                self.run_single_cycle(execute_approved=True)
            except Exception as exc:
                logger.error(f"Feil i autonom intradag-løkke: {exc}")
            for _ in range(self.interval_sec):
                if not self.running:
                    break
                time.sleep(1)

    def get_status(self) -> Dict[str, Any]:
        from alpaca_execution import AlpacaClient

        try:
            alp = AlpacaClient().get_portfolio_snapshot()
        except Exception:
            alp = {"connected": False, "equity": 100000.0, "cash": 100000.0, "today_pnl": 0.0, "today_pnl_pct": 0.0, "positions": []}

        eq = float(alp.get("equity") or 100000.0)
        cash = float(alp.get("cash") or 0.0)
        max_alloc = eq * self.specialist.max_portfolio_pct
        eff_budget = min(max_alloc, max(0.0, cash))

        return {
            "success": True,
            "autonomous_running": self.running,
            "max_portfolio_pct": round(self.specialist.max_portfolio_pct * 100.0, 1),
            "interval_sec": self.interval_sec,
            "last_scan_at": self.last_scan_at,
            "window": self.specialist.check_eod_time_window(),
            "alpaca_paper": {
                "connected": bool(alp.get("connected")),
                "equity_usd": round(eq, 2),
                "cash_usd": round(cash, 2),
                "max_10pct_usd": round(max_alloc, 2),
                "effective_budget_usd": round(eff_budget, 2),
                "today_pnl_usd": round(float(alp.get("today_pnl") or 0.0), 2),
                "today_pnl_pct": round(float(alp.get("today_pnl_pct") or 0.0), 2),
            },
            "today_summary": self.specialist.get_today_summary(),
            "last_candidates": self.last_candidates,
        }


INTRADAY_RUNNER = AutonomousIntradayRunner()


if __name__ == "__main__":
    agent = USIntradaySpecialist(max_portfolio_pct=0.10)
    sig = IntradaySignal("NVDA", "ORB_BREAKOUT_15M", "BUY", 145.0, 143.20, 149.50, 2.4, 144.10)
    print(json.dumps(agent.generate_intraday_decision(sig, 50000.0, 12500.0), indent=2))
