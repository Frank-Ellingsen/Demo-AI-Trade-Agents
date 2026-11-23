#!/usr/bin/env python3
"""
us_intraday_options_engine.py
=============================================================================
AUTONOM INTRADAG OPSJONS- & AKSJEAGENT (ALPACA PAPER V2)
Med toveis handel (Calls & Puts), hardt tak på $5 000 max allokering,
Hurdle Rate Gatekeeper (Spread + Gebyrer + 37,84 % skatt),
og integrert DuckDB Backtracking & Performance Analytics Engine.
=============================================================================
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import duckdb
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("USOptionsIntraday")

MAX_ALLOCATION_USD = 5000.00
PORTFOLIO_CAP_PCT = 0.10
TAX_RATE_NORWAY = 0.3784
MIN_NET_PROFIT_PCT = 0.0075
MIN_RISK_REWARD = 2.00
DEFAULT_SPREAD_SLIPPAGE_USD = 0.10

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = str(BASE_DIR / "data" / "options_intraday_trades.duckdb")


@dataclass
class OptionSignal:
    ticker: str
    underlying_price: float
    direction: str                 # "BULLISH_CALL" eller "BEARISH_PUT"
    strategy_name: str             # "15M_ORB_BREAKOUT" eller "VWAP_REJECTION"
    underlying_stop: float
    underlying_target: float
    strike: float
    dte_days: int
    option_type: str               # "call" eller "put"
    option_market_price: float     # Mid-price
    estimated_delta: float         # 0.65 - 0.75


class OptionsBacktrackingVault:
    """DuckDB Kunnskapsbase og Backtracking-journal for intradag opsjonshandler."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self.conn = duckdb.connect(self.db_path)
        self._init_tables()

    def _init_tables(self) -> None:
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS options_trade_journal (
                trade_id VARCHAR PRIMARY KEY,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                ticker VARCHAR NOT NULL,
                direction VARCHAR NOT NULL,
                strategy VARCHAR NOT NULL,
                strike DOUBLE NOT NULL,
                option_type VARCHAR NOT NULL,
                contracts_count INTEGER NOT NULL,
                entry_premium DOUBLE NOT NULL,
                invested_capital_usd DOUBLE NOT NULL,
                stop_loss_premium DOUBLE NOT NULL,
                target_premium DOUBLE NOT NULL,
                exit_premium DOUBLE,
                exit_reason VARCHAR,
                realized_pnl_usd DOUBLE,
                after_tax_pnl_usd DOUBLE,
                friction_paid_usd DOUBLE,
                breakeven_triggered BOOLEAN DEFAULT FALSE,
                status VARCHAR DEFAULT 'OPEN'
            );
        """)

    def log_trade_entry(self, trade_data: Dict[str, Any]) -> str:
        trade_id = f"OPT-{trade_data['ticker']}-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4].upper()}"
        self.conn.execute(
            """
            INSERT INTO options_trade_journal (
                trade_id, ticker, direction, strategy, strike, option_type,
                contracts_count, entry_premium, invested_capital_usd,
                stop_loss_premium, target_premium, friction_paid_usd, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN')
            """,
            [
                trade_id,
                trade_data["ticker"],
                trade_data["direction"],
                trade_data["strategy"],
                trade_data["strike"],
                trade_data["option_type"],
                trade_data["contracts"],
                trade_data["entry_premium"],
                trade_data["invested_usd"],
                trade_data["stop_loss"],
                trade_data["target"],
                trade_data["total_friction"],
            ],
        )
        return trade_id

    def close_trade_backtrack(
        self,
        trade_id: str,
        exit_premium: float,
        exit_reason: str,
        breakeven_triggered: bool = False,
    ) -> None:
        row = self.conn.execute(
            "SELECT contracts_count, entry_premium, friction_paid_usd FROM options_trade_journal WHERE trade_id = ?",
            [trade_id],
        ).fetchone()
        if not row:
            return
        contracts, entry_p, friction = row
        gross_pnl = (exit_premium - entry_p) * 100 * contracts
        net_pnl = gross_pnl - friction
        after_tax_pnl = net_pnl * (1.0 - TAX_RATE_NORWAY) if net_pnl > 0 else net_pnl

        self.conn.execute(
            """
            UPDATE options_trade_journal
            SET exit_premium = ?, exit_reason = ?, realized_pnl_usd = ?,
                after_tax_pnl_usd = ?, breakeven_triggered = ?, status = 'CLOSED'
            WHERE trade_id = ?
            """,
            [exit_premium, exit_reason, round(net_pnl, 2), round(after_tax_pnl, 2), breakeven_triggered, trade_id],
        )

    def get_recent_trades(self, limit: int = 20) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT trade_id, CAST(timestamp AS VARCHAR), ticker, direction, strategy, strike,
                   option_type, contracts_count, entry_premium, invested_capital_usd,
                   stop_loss_premium, target_premium, exit_premium, exit_reason,
                   realized_pnl_usd, after_tax_pnl_usd, friction_paid_usd,
                   breakeven_triggered, status
            FROM options_trade_journal
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            [limit],
        ).fetchall()
        cols = [
            "trade_id", "timestamp", "ticker", "direction", "strategy", "strike",
            "option_type", "contracts_count", "entry_premium", "invested_capital_usd",
            "stop_loss_premium", "target_premium", "exit_premium", "exit_reason",
            "realized_pnl_usd", "after_tax_pnl_usd", "friction_paid_usd",
            "breakeven_triggered", "status",
        ]
        return [dict(zip(cols, r)) for r in rows]

    def get_backtracking_summary(self) -> Dict[str, Any]:
        """Beregner akkumulert statistikk, win-rate og realisert PnL etter skatt."""
        res = self.conn.execute("""
            SELECT
                COUNT(*) as total_trades,
                COALESCE(SUM(CASE WHEN realized_pnl_usd > 0 THEN 1 ELSE 0 END), 0) as wins,
                COALESCE(SUM(CASE WHEN breakeven_triggered THEN 1 ELSE 0 END), 0) as be_saves,
                COALESCE(SUM(realized_pnl_usd), 0.0) as total_net_pnl,
                COALESCE(SUM(after_tax_pnl_usd), 0.0) as total_after_tax_pnl,
                COALESCE(SUM(friction_paid_usd), 0.0) as total_friction
            FROM options_trade_journal
            WHERE status = 'CLOSED'
        """).fetchone()

        total = int(res[0] or 0)
        if total == 0:
            return {
                "total_trades": 0,
                "win_rate_pct": 0.0,
                "breakeven_saves": 0,
                "total_net_pnl_usd": 0.0,
                "total_after_tax_pnl_usd": 0.0,
                "total_friction_usd": 0.0,
            }
        return {
            "total_trades": total,
            "win_rate_pct": round((int(res[1]) / total) * 100.0, 1),
            "breakeven_saves": int(res[2]),
            "total_net_pnl_usd": round(float(res[3]), 2),
            "total_after_tax_pnl_usd": round(float(res[4]), 2),
            "total_friction_usd": round(float(res[5]), 2),
        }

    def close(self) -> None:
        self.conn.close()


class USOptionsIntradayAgent:
    """Spesialistagent for toveis intradag-opsjoner med Alpaca Paper og Hurdle Gatekeeper."""

    def __init__(self, db_path: str = DB_PATH):
        self.vault = OptionsBacktrackingVault(db_path)
        self.alpaca_key = os.getenv("ALPACA_API_KEY", "")
        self.alpaca_secret = os.getenv("ALPACA_SECRET_KEY", "")
        self.paper_url = "https://paper-api.alpaca.markets"

    def calculate_position_sizing(self, option_premium: float, portfolio_value_usd: float, free_cash_usd: float) -> int:
        """Sikrer at allokeringen ALDRI overstiger $5 000 USD, maks 10 % og ledig cash."""
        max_allowed = min(MAX_ALLOCATION_USD, portfolio_value_usd * PORTFOLIO_CAP_PCT, max(0.0, free_cash_usd))
        contract_cost = option_premium * 100.0
        if contract_cost <= 0 or max_allowed < contract_cost:
            return 0
        return int(max_allowed // contract_cost)

    def evaluate_hurdle_rate(self, signal: OptionSignal, contracts: int) -> Dict[str, Any]:
        """Beregner realistisk friksjon og 37,84 % skatt. Setter VETO hvis netto er for lav."""
        if contracts <= 0:
            return {
                "decision": "VETO_CAPITAL_EXCEEDED",
                "reason": "Posisjonen overskrider $5 000 maksgrensen eller mangler ledig cash.",
            }

        invested_usd = signal.option_market_price * 100 * contracts
        delta = abs(signal.estimated_delta)
        underlying_move_up = abs(signal.underlying_target - signal.underlying_price)
        underlying_move_down = abs(signal.underlying_price - signal.underlying_stop)

        target_premium = signal.option_market_price + (underlying_move_up * delta)
        stop_premium = max(0.05, signal.option_market_price - (underlying_move_down * delta))

        spread_friction = DEFAULT_SPREAD_SLIPPAGE_USD * 100 * contracts
        reg_fees = (0.02 + 0.035) * 2 * contracts
        total_friction = spread_friction + reg_fees

        gross_profit_usd = (target_premium - signal.option_market_price) * 100 * contracts
        net_profit_before_tax = gross_profit_usd - total_friction
        tax_amount = net_profit_before_tax * TAX_RATE_NORWAY if net_profit_before_tax > 0 else 0.0
        net_profit_after_tax = net_profit_before_tax - tax_amount

        total_risk_usd = (signal.option_market_price - stop_premium) * 100 * contracts + total_friction
        rr_ratio = net_profit_before_tax / total_risk_usd if total_risk_usd > 0 else 0.0
        net_return_pct = net_profit_before_tax / invested_usd if invested_usd > 0 else 0.0

        if net_return_pct < MIN_NET_PROFIT_PCT:
            return {
                "decision": "VETO_HURDLE_TOO_LOW",
                "reason": f"Netto forventet avkastning ({net_return_pct * 100:.2f}%) slår ikke friksjons- og skattekravet.",
                "friction_usd": round(total_friction, 2),
            }

        if rr_ratio < MIN_RISK_REWARD:
            return {
                "decision": "VETO_POOR_RISK_REWARD",
                "reason": f"Risk/Reward ({rr_ratio:.2f}x) er lavere enn minstekravet på {MIN_RISK_REWARD}x.",
                "rr_ratio": round(rr_ratio, 2),
            }

        return {
            "decision": "APPROVED_FOR_EXECUTION",
            "ticker": signal.ticker.upper(),
            "direction": signal.direction,
            "option_type": signal.option_type,
            "strike": signal.strike,
            "contracts": contracts,
            "invested_usd": round(invested_usd, 2),
            "entry_premium": round(signal.option_market_price, 2),
            "stop_loss_premium": round(stop_premium, 2),
            "target_premium": round(target_premium, 2),
            "total_friction_usd": round(total_friction, 2),
            "gross_profit_usd": round(gross_profit_usd, 2),
            "tax_usd": round(tax_amount, 2),
            "net_profit_after_tax_usd": round(net_profit_after_tax, 2),
            "net_return_pct": round(net_return_pct * 100.0, 2),
            "risk_reward_ratio": round(rr_ratio, 2),
        }

    def process_live_signal(self, signal: OptionSignal, portfolio_val: float, free_cash: float, stage_in_vault: bool = True) -> Dict[str, Any]:
        """Full behandlingsflyt for et innkommende intradag-opsjonssignal."""
        contracts = self.calculate_position_sizing(signal.option_market_price, portfolio_val, free_cash)
        eval_res = self.evaluate_hurdle_rate(signal, contracts)

        if eval_res["decision"] != "APPROVED_FOR_EXECUTION":
            return eval_res

        if stage_in_vault:
            trade_id = self.vault.log_trade_entry({
                "ticker": signal.ticker.upper(),
                "direction": signal.direction,
                "strategy": signal.strategy_name,
                "strike": signal.strike,
                "option_type": signal.option_type,
                "contracts": contracts,
                "entry_premium": eval_res["entry_premium"],
                "invested_usd": eval_res["invested_usd"],
                "stop_loss": eval_res["stop_loss_premium"],
                "target": eval_res["target_premium"],
                "total_friction": eval_res["total_friction_usd"],
            })
            eval_res["trade_id"] = trade_id
            eval_res["status"] = "ORDER_STAGED_ALPACA_PAPER"
        return eval_res

    def close(self) -> None:
        self.vault.close()


def detect_live_bull_bear_signal(ticker: str, dte_days: int = 4, vol: float = 0.42) -> Dict[str, Any]:
    """Henter live 5m intradag-barer og detekterer automatisk BULL (BUY_CALL) eller BEAR (BUY_PUT)."""
    import requests
    from src.analytics.advanced_cockpit import black_scholes_greeks

    sym = (ticker or "NVDA").strip().upper()
    hdr = {"User-Agent": "Mozilla/5.0"}
    r = requests.get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
        params={"range": "1d", "interval": "5m"},
        headers=hdr,
        timeout=6,
    )
    if r.status_code != 200:
        raise ValueError(f"Yahoo Finance svarte HTTP {r.status_code} for {sym}")
    res_list = (r.json().get("chart") or {}).get("result") or []
    if not res_list:
        raise ValueError(f"Ingen intradag-data funnet for {sym}")
    res0 = res_list[0]
    q = res0.get("indicators", {}).get("quote", [{}])[0]
    highs = [float(x) for x in (q.get("high") or []) if x is not None]
    lows = [float(x) for x in (q.get("low") or []) if x is not None]
    closes = [float(x) for x in (q.get("close") or []) if x is not None]
    vols = [float(x or 0) for x in (q.get("volume") or [])]
    if len(closes) < 3:
        raise ValueError(f"For få 5m-barer for {sym}")

    spot = float((res0.get("meta") or {}).get("regularMarketPrice") or closes[-1])
    orb_high = max(highs[:3])
    orb_low = min(lows[:3])
    orb_range = max(orb_high - orb_low, spot * 0.008)

    n = min(len(closes), len(vols))
    tot_v = sum(vols[:n])
    vwap = sum(closes[i] * vols[i] for i in range(n)) / tot_v if tot_v > 0 else spot
    avg_v = (tot_v / n) if n > 0 else 1.0
    vol_ratio = (vols[n - 1] / avg_v) if avg_v > 0 else 1.0

    # Auto Bull vs. Bear regime detection
    if spot > orb_high and spot >= vwap:
        regime, side, opt_type, strat = "BULL_BREAKOUT", "BUY_CALL", "call", "15M_ORB_BREAKOUT"
        reason = f"Kurs (${spot:.2f}) brøt OVER 15m ORB High (${orb_high:.2f}) og VWAP (${vwap:.2f}) · Volum {vol_ratio:.1f}x."
    elif spot < orb_low and spot <= vwap:
        regime, side, opt_type, strat = "BEAR_BREAKDOWN", "BUY_PUT", "put", "15M_ORB_BREAKDOWN"
        reason = f"Kurs (${spot:.2f}) brøt UNDER 15m ORB Low (${orb_low:.2f}) og VWAP (${vwap:.2f}) · Volum {vol_ratio:.1f}x."
    elif spot >= vwap:
        regime, side, opt_type, strat = "BULL_VWAP_BIAS", "BUY_CALL", "call", "VWAP_PULLBACK_CALL"
        reason = f"Kurs (${spot:.2f}) handler OVER intradag VWAP (${vwap:.2f}) — velger automatisk BULL (BUY CALL)."
    else:
        regime, side, opt_type, strat = "BEAR_VWAP_BIAS", "BUY_PUT", "put", "VWAP_REJECTION_PUT"
        reason = f"Kurs (${spot:.2f}) handler UNDER intradag VWAP (${vwap:.2f}) — velger automatisk BEAR (BUY PUT)."

    step = 1.0 if spot < 200 else 2.5
    if opt_type == "call":
        strike = round(math.floor(spot / step) * step, 2)
        u_stop = round(min(spot - orb_range * 0.65, orb_low), 2)
        risk = max(spot - u_stop, spot * 0.008)
        u_target = round(spot + risk * 2.45, 2)
    else:
        strike = round(math.ceil(spot / step) * step, 2)
        u_stop = round(max(spot + orb_range * 0.65, orb_high), 2)
        risk = max(u_stop - spot, spot * 0.008)
        u_target = round(spot - risk * 2.45, 2)

    g = black_scholes_greeks(spot, strike, dte_days, vol, opt_type=opt_type)
    mid = max(0.50, round(g["price"], 2))
    ask = round(mid + 0.05, 2)
    bid = round(max(0.05, mid - 0.05), 2)

    return {
        "success": True,
        "ticker": sym,
        "regime": regime,
        "side": side,
        "option_type": opt_type,
        "strategy": strat,
        "reason": reason,
        "underlying_price": round(spot, 2),
        "underlying_stop": u_stop,
        "underlying_target": u_target,
        "contract_strike": strike,
        "contract_ask": ask,
        "contract_bid": bid,
        "delta": round(g["delta"], 2),
        "days_to_expiry": dte_days,
        "vwap": round(vwap, 2),
        "orb_high": round(orb_high, 2),
        "orb_low": round(orb_low, 2),
        "volume_ratio": round(vol_ratio, 2),
    }


class AutonomousOptionsRunner:
    """Autonom toveis Call/Put Intradag Opsjonsagent (kun Alpaca Paper + DuckDB Vault + Telegram)."""

    def __init__(self) -> None:
        self.running = False
        self.interval_sec = 240
        self.last_scan_at = ""
        self.last_signals: List[Dict[str, Any]] = []
        self._thread = None

    def start(self, interval_sec: int = 240) -> Dict[str, Any]:
        import threading
        from us_intraday_agent import send_telegram_intraday

        self.interval_sec = max(60, min(900, int(interval_sec)))
        if not self.running:
            self.running = True
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
            send_telegram_intraday(
                "⚡ <b>AUTONOM TOVEIS OPSJONSAGENT AKTIVERT (Alpaca Paper)</b>\n"
                "• Velger automatisk <b>CALL (Bull)</b> eller <b>PUT (Bear)</b> fra 5m ORB/VWAP\n"
                "• Hardt tak: <b>$5 000 / 3 %</b> · Hurdle: <b>R/R ≥ 2.0x</b> etter spread &amp; 37.84% skatt"
            )
        return self.get_status()

    def stop(self) -> Dict[str, Any]:
        from us_intraday_agent import send_telegram_intraday

        was = self.running
        self.running = False
        if was:
            send_telegram_intraday("⏸️ <b>AUTONOM TOVEIS OPSJONSAGENT SATT PÅ PAUSE</b>")
        return self.get_status()

    def run_single_cycle(self, stage_approved: bool = True) -> Dict[str, Any]:
        import time as _time
        from alpaca_execution import AlpacaClient
        from us_intraday_agent import USIntradaySpecialist, send_telegram_intraday

        win = USIntradaySpecialist().check_eod_time_window()
        try:
            alp = AlpacaClient().get_portfolio_snapshot()
        except Exception:
            alp = {"equity": 100000.0, "cash": 25000.0}
        eq = float(alp.get("equity") or 100000.0)
        cash = float(alp.get("cash") or 25000.0)

        results = []
        staged = []
        eng = USOptionsIntradayAgent()
        try:
            for sym in ("NVDA", "AAPL", "MSFT", "TSLA", "SPY", "QQQ"):
                try:
                    det = detect_live_bull_bear_signal(sym)
                    sig = OptionSignal(
                        ticker=sym,
                        underlying_price=det["underlying_price"],
                        direction="BULLISH_CALL" if det["side"] == "BUY_CALL" else "BEARISH_PUT",
                        strategy_name=det["strategy"],
                        underlying_stop=det["underlying_stop"],
                        underlying_target=det["underlying_target"],
                        strike=det["contract_strike"],
                        dte_days=det["days_to_expiry"],
                        option_type=det["option_type"],
                        option_market_price=(det["contract_ask"] + det["contract_bid"]) / 2.0,
                        estimated_delta=det["delta"],
                    )
                    do_stage = stage_approved and win["allow_new_entries"] and det["regime"] in ("BULL_BREAKOUT", "BEAR_BREAKDOWN")
                    ev = eng.process_live_signal(sig, eq, cash, stage_in_vault=do_stage)
                    ev["regime"] = det["regime"]
                    ev["reason_detect"] = det["reason"]
                    results.append(ev)
                    if do_stage and ev.get("trade_id"):
                        staged.append(ev)
                        send_telegram_intraday(
                            f"⚡ <b>AUTONOM OPSJON ({ev['direction']}) — {sym}</b>\n"
                            f"• {ev['contracts']} stk {ev['option_type'].upper()} K=${ev['strike']} (${ev['invested_usd']:,.2f})\n"
                            f"• R/R: <b>{ev['risk_reward_ratio']}x</b> · Netto e. 37.84% skatt: <b>+${ev['net_profit_after_tax_usd']:.2f}</b>\n"
                            f"• Vault Ref: <code>{ev['trade_id']}</code>"
                        )
                except Exception:
                    continue
        finally:
            eng.close()

        self.last_scan_at = _time.strftime("%H:%M:%S")
        self.last_signals = results
        return {"success": True, "scanned_at": self.last_scan_at, "signals": results, "staged": staged}

    def _loop(self) -> None:
        import time as _time

        while self.running:
            try:
                self.run_single_cycle(stage_approved=True)
            except Exception as exc:
                logger.error(f"Feil i autonom opsjonsløkke: {exc}")
            for _ in range(self.interval_sec):
                if not self.running:
                    break
                _time.sleep(1)

    def get_status(self) -> Dict[str, Any]:
        vault = OptionsBacktrackingVault()
        try:
            summary = vault.get_backtracking_summary()
            trades = vault.get_recent_trades(limit=15)
        finally:
            vault.close()
        return {
            "success": True,
            "autonomous_running": self.running,
            "last_scan_at": self.last_scan_at,
            "summary": summary,
            "trades": trades,
            "last_signals": self.last_signals,
        }


OPTIONS_RUNNER = AutonomousOptionsRunner()


if __name__ == "__main__":
    agent = USOptionsIntradayAgent()
    call_sig = OptionSignal("NVDA", 145.0, "BULLISH_CALL", "15M_ORB_BREAKOUT", 143.5, 149.0, 144.0, 4, "call", 4.20, 0.72)
    res = agent.process_live_signal(call_sig, 85000.0, 25000.0)
    print("NVDA CALL:", res)
    if res.get("trade_id"):
        agent.vault.close_trade_backtrack(res["trade_id"], res["target_premium"], "TAKE_PROFIT_LIMIT")
    print("SUMMARY:", agent.vault.get_backtracking_summary())
    agent.close()
