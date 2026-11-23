#!/usr/bin/env python3
"""
WEEKLY AI SIGNAL AUDIT & ATTRIBUTION REPORT (DUCKDB)
====================================================
Kjører ukentlig eller månedlig revisjon av AI-agentens beslutninger,
kontrafaktisk skyggesporing (A/B-testing), XAI avvisningsdiagnose,
alfa-dekomponering mot S&P 500 (SPY) og norsk skattestatus (37.84 %).
"""

import os
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Any, List

import duckdb

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("SignalAudit")

BASE_DIR = Path(__file__).resolve().parent


def _runtime_db_path(filename: str, env_var: str) -> str:
    env_val = os.getenv(env_var)
    if env_val:
        Path(env_val).parent.mkdir(parents=True, exist_ok=True)
        return env_val
    local_app = os.getenv("LOCALAPPDATA")
    if os.name == "nt" and local_app:
        p = Path(local_app) / "ai_trader_runtime"
        p.mkdir(parents=True, exist_ok=True)
        return str(p / filename)
    p = BASE_DIR / "data"
    p.mkdir(parents=True, exist_ok=True)
    return str(p / filename)


DB_PATH = _runtime_db_path("weekly_signal_audit.duckdb", "SIGNAL_AUDIT_DB_PATH")
TAX_DB_PATH = _runtime_db_path("tax_tracking.duckdb", "TAX_DB_PATH")


def ensure_audit_tables(conn: duckdb.DuckDBPyConnection):
    """Sørger for at tabellene finnes, og legger inn demodata dersom de er tomme."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS strategy_evaluations (
            evaluation_id VARCHAR PRIMARY KEY,
            timestamp TIMESTAMP,
            ticker VARCHAR,
            strategy_name VARCHAR,
            direction VARCHAR,
            instrument_type VARCHAR,
            underlying_price DOUBLE,
            target_delta DOUBLE,
            option_strike DOUBLE,
            contracts_or_shares INTEGER,
            entry_limit DOUBLE,
            stop_loss DOUBLE,
            take_profit DOUBLE,
            hurdle_spread_bps DOUBLE,
            hurdle_cleared BOOLEAN,
            ai_decision VARCHAR,
            rejection_reason VARCHAR,
            xai_confidence DOUBLE,
            hypothetical_net_return DOUBLE,
            hit_stop_loss BOOLEAN
        );
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS closed_trades (
            trade_id VARCHAR PRIMARY KEY,
            ticker VARCHAR,
            entry_date DATE,
            exit_date DATE,
            direction VARCHAR,
            qty INTEGER,
            entry_price DOUBLE,
            exit_price DOUBLE,
            gross_pnl_usd DOUBLE,
            net_pnl_usd DOUBLE,
            realized_pnl_pct DOUBLE,
            spy_return_pct DOUBLE,
            status VARCHAR
        );
    """)

    count = conn.execute("SELECT COUNT(*) FROM strategy_evaluations").fetchone()[0]
    if count == 0:
        sample_data = [
            ("EV-001", "2026-11-10 14:45:00", "NVDA", "15m_ORB", "BUY", "OPTION_CALL", 142.5, 0.68, 140.0, 5, 4.80, 3.60, 6.80, 8.5, True, "APPROVED", "NONE", 0.88, 0.225, False),
            ("EV-002", "2026-11-11 15:15:00", "TSLA", "VWAP_REVERSAL", "SELL", "OPTION_PUT", 215.0, -0.65, 220.0, 4, 5.20, 4.00, 7.50, 9.0, True, "APPROVED", "NONE", 0.79, 0.180, False),
            ("EV-003", "2026-11-12 14:35:00", "AMD", "15m_ORB", "BUY", "STOCK", 138.0, 1.0, 0.0, 35, 138.2, 135.5, 143.0, 4.2, False, "REJECTED", "EARNINGS_TRAP", 0.92, -0.048, True),
            ("EV-004", "2026-11-13 15:00:00", "AAPL", "VWAP_PULLBACK", "BUY", "OPTION_CALL", 228.0, 0.70, 225.0, 6, 4.10, 3.10, 6.00, 6.5, False, "REJECTED", "HIGH_VIX_CHOP", 0.84, -0.035, True),
            ("EV-005", "2026-11-14 14:50:00", "MSFT", "15m_ORB", "BUY", "STOCK", 418.0, 1.0, 0.0, 11, 418.5, 412.0, 429.0, 3.8, True, "APPROVED", "NONE", 0.85, 0.021, False),
            ("EV-006", "2026-11-17 15:30:00", "META", "VWAP_REVERSAL", "BUY", "OPTION_CALL", 580.0, 0.69, 575.0, 2, 8.50, 6.50, 12.00, 7.2, False, "REJECTED", "SPREAD_TOO_WIDE", 0.75, 0.015, False),
            ("EV-007", "2026-11-18 14:40:00", "PLTR", "15m_ORB", "BUY", "STOCK", 62.0, 1.0, 0.0, 75, 62.1, 59.8, 66.5, 5.0, True, "APPROVED", "NONE", 0.91, 0.052, False),
            ("EV-008", "2026-11-19 15:10:00", "AMZN", "15m_ORB", "BUY", "OPTION_CALL", 202.0, 0.67, 200.0, 7, 3.80, 2.90, 5.50, 6.0, False, "REJECTED", "EARNINGS_TRAP", 0.89, -0.062, True),
            ("EV-009", "2026-11-20 14:45:00", "GOOGL", "VWAP_PULLBACK", "BUY", "STOCK", 175.0, 1.0, 0.0, 28, 175.2, 171.5, 181.0, 4.0, True, "APPROVED", "NONE", 0.82, -0.018, True),
            ("EV-010", "2026-11-21 15:05:00", "SMCI", "15m_ORB", "SELL", "OPTION_PUT", 28.0, -0.66, 30.0, 15, 2.40, 1.80, 3.50, 12.0, False, "REJECTED", "EXCESSIVE_VOLATILITY", 0.95, -0.110, True),
        ]
        conn.executemany(
            "INSERT INTO strategy_evaluations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            sample_data,
        )

    trade_count = conn.execute("SELECT COUNT(*) FROM closed_trades").fetchone()[0]
    if trade_count == 0:
        sample_trades = [
            ("TR-001", "NVDA", "2026-11-10", "2026-11-11", "BUY", 5, 4.80, 5.88, 540.0, 532.5, 22.5, 1.2, "CLOSED"),
            ("TR-002", "TSLA", "2026-11-11", "2026-11-12", "SELL", 4, 5.20, 6.14, 376.0, 368.0, 18.0, -0.8, "CLOSED"),
            ("TR-003", "MSFT", "2026-11-14", "2026-11-17", "BUY", 11, 418.5, 427.3, 96.8, 93.5, 2.1, 0.5, "CLOSED"),
            ("TR-004", "PLTR", "2026-11-18", "2026-11-19", "BUY", 75, 62.1, 65.3, 240.0, 237.0, 5.2, 1.1, "CLOSED"),
            ("TR-005", "GOOGL", "2026-11-20", "2026-11-21", "BUY", 28, 175.2, 172.0, -89.6, -92.0, -1.8, -0.4, "CLOSED"),
        ]
        conn.executemany(
            "INSERT INTO closed_trades VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            sample_trades,
        )


def get_weekly_signal_audit_payload(db_path: str = DB_PATH) -> Dict[str, Any]:
    """Returnerer strukturert JSON-rapport for 08 Post-Mortem og API (/api/analytics/signal-audit)."""
    with duckdb.connect(db_path) as conn:
        ensure_audit_tables(conn)

        cohorts_rows = conn.execute("""
            SELECT
                CASE
                    WHEN ai_decision = 'APPROVED' THEN 'AI Godkjent (Eksekvert)'
                    WHEN ai_decision = 'REJECTED' THEN 'AI Avvist (Shadow Tracking)'
                    ELSE ai_decision
                END AS kohort,
                COUNT(*) AS total_signaler,
                ROUND(AVG(CASE WHEN hypothetical_net_return > 0 THEN 1.0 ELSE 0.0 END) * 100, 1) AS vinnrate_pct,
                ROUND(AVG(hypothetical_net_return) * 100, 2) AS snitt_netto_pct,
                ROUND(AVG(CASE WHEN hit_stop_loss THEN 1.0 ELSE 0.0 END) * 100, 1) AS stop_loss_pct
            FROM strategy_evaluations
            GROUP BY ai_decision
            ORDER BY ai_decision ASC
        """).fetchall()

        base_row = conn.execute("""
            SELECT
                'Totalt Alle Signaler (Baseline)' AS kohort,
                COUNT(*) AS total_signaler,
                ROUND(AVG(CASE WHEN hypothetical_net_return > 0 THEN 1.0 ELSE 0.0 END) * 100, 1) AS vinnrate_pct,
                ROUND(AVG(hypothetical_net_return) * 100, 2) AS snitt_netto_pct,
                ROUND(AVG(CASE WHEN hit_stop_loss THEN 1.0 ELSE 0.0 END) * 100, 1) AS stop_loss_pct
            FROM strategy_evaluations
        """).fetchone()

        cohorts: List[Dict[str, Any]] = [
            {"cohort": r[0], "signals": int(r[1]), "win_rate_pct": float(r[2]), "avg_net_pct": float(r[3]), "stop_loss_pct": float(r[4])}
            for r in list(cohorts_rows) + ([base_row] if base_row else [])
        ]

        xai_rows = conn.execute("""
            SELECT
                rejection_reason,
                COUNT(*) AS blocked_count,
                ROUND(AVG(hypothetical_net_return) * 100, 2) AS hyp_avg_pct,
                ROUND(AVG(CASE WHEN hit_stop_loss THEN 1.0 ELSE 0.0 END) * 100, 1) AS sl_hit_pct,
                CASE
                    WHEN AVG(hypothetical_net_return) < -0.02 THEN 'Kritisk filter (Unngikk stort tap)'
                    WHEN AVG(hypothetical_net_return) < 0 THEN 'Godkjent (Unngikk tap)'
                    ELSE 'Overvåk (Tapte mulig gevinst)'
                END AS verdict
            FROM strategy_evaluations
            WHERE ai_decision = 'REJECTED'
            GROUP BY rejection_reason
            ORDER BY blocked_count DESC, hyp_avg_pct ASC
        """).fetchall()
        xai_rejections = [
            {"reason": r[0], "blocked_count": int(r[1]), "hyp_avg_pct": float(r[2]), "sl_hit_pct": float(r[3]), "verdict": r[4]}
            for r in xai_rows
        ]

        alpha_rows = conn.execute("""
            SELECT
                ticker,
                CAST(entry_date AS VARCHAR),
                direction,
                realized_pnl_pct,
                spy_return_pct,
                ROUND(realized_pnl_pct - spy_return_pct, 2) AS pure_alpha_pct,
                net_pnl_usd
            FROM closed_trades
            ORDER BY entry_date DESC
        """).fetchall()
        alpha_trades = [
            {
                "ticker": r[0], "entry_date": r[1], "direction": r[2],
                "strategy_pct": float(r[3]), "spy_pct": float(r[4]),
                "pure_alpha_pct": float(r[5]), "net_pnl_usd": float(r[6]),
            }
            for r in alpha_rows
        ]

        sum_row = conn.execute("""
            SELECT
                COUNT(*),
                ROUND(COALESCE(SUM(net_pnl_usd), 0.0), 2),
                ROUND(COALESCE(AVG(realized_pnl_pct), 0.0), 2),
                ROUND(COALESCE(AVG(spy_return_pct), 0.0), 2),
                ROUND(COALESCE(AVG(realized_pnl_pct - spy_return_pct), 0.0), 2)
            FROM closed_trades
        """).fetchone()

    return {
        "success": True,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "db_path": db_path,
        "cohorts": cohorts,
        "xai_rejections": xai_rejections,
        "alpha_trades": alpha_trades,
        "alpha_summary": {
            "closed_trades": int(sum_row[0] or 0),
            "total_net_pnl_usd": float(sum_row[1] or 0.0),
            "avg_strategy_pct": float(sum_row[2] or 0.0),
            "avg_spy_pct": float(sum_row[3] or 0.0),
            "avg_pure_alpha_pct": float(sum_row[4] or 0.0),
        },
    }


def main():
    payload = get_weekly_signal_audit_payload()
    print("\n" + "#" * 80)
    print(f" UKENTLIG AI-SIGNAL REVISJON & SPY ALFA-ATTRIBUSJON ({payload['generated_at']})")
    print("#" * 80)
    print("\nDEL 1 — A/B COHORT SHADOW TRACKING:")
    for c in payload["cohorts"]:
        print(f"  {c['cohort']:32} | n={c['signals']:2} | Win={c['win_rate_pct']:5.1f}% | Snitt={c['avg_net_pct']:+6.2f}% | SL={c['stop_loss_pct']:5.1f}%")
    print("\nDEL 2 — XAI AVVISNINGSDIAGNOSE:")
    for x in payload["xai_rejections"]:
        print(f"  {x['reason']:22} | n={x['blocked_count']:2} | Hyp={x['hyp_avg_pct']:+6.2f}% | {x['verdict']}")
    s = payload["alpha_summary"]
    print(f"\nDEL 3 — SPY ALFA: {s['closed_trades']} handler | Netto=${s['total_net_pnl_usd']:+,.2f} | Snitt Alfa={s['avg_pure_alpha_pct']:+.2f}%\n")


if __name__ == "__main__":
    main()
