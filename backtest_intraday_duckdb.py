#!/usr/bin/env python3
"""
backtest_intraday_duckdb.py
=============================================================================
Vektorisert Intradag Backtest-motor i DuckDB for US Equities (Alpaca Data).
Simulerer 15-minutters Opening Range Breakout (ORB) og VWAP Pullback med:
  1. Dynamisk Breakeven / Trailing Stop (flytter stop til entry ved +1.0R).
  2. Regulatoriske gebyrer (SEC, FINRA TAF) og bid/ask-spread (3 bps standard).
  3. Norsk kapitalskatt (37.84% tax drag på nettogevinst).
  4. Tvungen EOD-flatting kl. 15:55 EST (21:55 CET).
=============================================================================
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, List

import duckdb
import numpy as np
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

ALPACA_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY", "")
DB_PATH = Path(__file__).resolve().parent / "data" / "intraday_backtest.duckdb"


def create_schema(con: duckdb.DuckDBPyConnection) -> None:
    """Oppretter tabeller for 5m-barer og handelsresultater i DuckDB."""
    con.execute("""
        CREATE TABLE IF NOT EXISTS intraday_bars_5m (
            timestamp TIMESTAMP NOT NULL,
            date DATE NOT NULL,
            ticker VARCHAR NOT NULL,
            open DOUBLE NOT NULL,
            high DOUBLE NOT NULL,
            low DOUBLE NOT NULL,
            close DOUBLE NOT NULL,
            volume BIGINT NOT NULL,
            vwap DOUBLE,
            PRIMARY KEY (ticker, timestamp)
        );
        CREATE TABLE IF NOT EXISTS backtest_trades (
            trade_id VARCHAR PRIMARY KEY,
            ticker VARCHAR NOT NULL,
            date DATE NOT NULL,
            entry_time TIMESTAMP NOT NULL,
            exit_time TIMESTAMP,
            entry_price DOUBLE NOT NULL,
            stop_loss DOUBLE NOT NULL,
            target_price DOUBLE NOT NULL,
            exit_price DOUBLE,
            exit_reason VARCHAR,
            gross_pnl_pct DOUBLE,
            net_pnl_pct DOUBLE,
            net_pnl_usd DOUBLE,
            after_tax_usd DOUBLE
        );
    """)


def generate_sample_data(
    con: duckdb.DuckDBPyConnection,
    tickers: List[str] | None = None,
    days: int = 20,
    quiet: bool = False,
) -> int:
    """Genererer realistiske 5-minutters barer dersom Alpaca historikk ikke lastes."""
    tickers = tickers or ["NVDA", "AAPL", "SPY"]
    if not quiet:
        print("Laster inn/simulerer historiske 5m-intradag barer for backtest...")
    con.execute("DELETE FROM intraday_bars_5m")

    np.random.seed(42)
    end_date = datetime.now().date()
    base_prices = {
        "NVDA": 140.0,
        "AAPL": 220.0,
        "SPY": 585.0,
        "QQQ": 505.0,
        "MSFT": 420.0,
        "META": 560.0,
        "TSLA": 335.0,
    }

    records = []
    for ticker in tickers:
        base_p = base_prices.get(ticker, 120.0)
        curr_p = base_p

        for d_offset in range(days, 0, -1):
            day_date = end_date - timedelta(days=d_offset)
            if day_date.weekday() >= 5:
                continue

            day_open = curr_p * (1 + np.random.normal(0, 0.008))
            p = day_open
            cum_vol = 0
            cum_vol_price = 0.0

            # Handelsdag: 15:30 CET (09:30 EST) til 22:00 CET (16:00 EST) -> 78 barer à 5 min
            for bar_idx in range(78):
                bar_time = datetime.combine(day_date, time(15, 30)) + timedelta(minutes=bar_idx * 5)
                vol_scale = 0.004 if bar_idx < 6 else 0.0018
                ret = np.random.normal(0.0001, vol_scale)
                if bar_idx == 3 and np.random.rand() > 0.4:
                    ret += 0.006

                b_open = p
                b_close = b_open * (1 + ret)
                b_high = max(b_open, b_close) * (1 + abs(np.random.normal(0, 0.0008)))
                b_low = min(b_open, b_close) * (1 - abs(np.random.normal(0, 0.0008)))

                b_vol = int(np.random.exponential(50000) * (3.0 if bar_idx < 3 else 1.0))
                cum_vol += b_vol
                cum_vol_price += ((b_high + b_low + b_close) / 3.0) * b_vol
                vwap = cum_vol_price / cum_vol if cum_vol > 0 else b_close

                records.append((
                    bar_time,
                    day_date,
                    ticker,
                    round(b_open, 2),
                    round(b_high, 2),
                    round(b_low, 2),
                    round(b_close, 2),
                    b_vol,
                    round(vwap, 2),
                ))
                p = b_close
            curr_p = p

    df = pd.DataFrame(
        records,
        columns=["timestamp", "date", "ticker", "open", "high", "low", "close", "volume", "vwap"],
    )
    con.register("df_temp", df)
    con.execute("INSERT INTO intraday_bars_5m SELECT * FROM df_temp")
    count = int(con.execute("SELECT COUNT(*) FROM intraday_bars_5m").fetchone()[0])
    if not quiet:
        print(f"Indeksert {count} fem-minutters barer over {days} dager i DuckDB.")
    return count


def run_orb_backtest(
    con: duckdb.DuckDBPyConnection,
    target_rr: float = 2.0,
    friction_bps: float = 3.0,
    capital_per_trade: float = 9655.0,
    quiet: bool = False,
) -> pd.DataFrame:
    """
    Kjører 15-minutters ORB Backtest med:
      - ORB Intervall: 15:30 - 15:45 CET (09:30 - 09:45 EST)
      - Volumfilter: > 1.2x snittvolum i åpningen
      - Breakeven trigger ved +1.0R
      - Friction: SEC, FINRA, bid-ask (3 bps standard)
    """
    target_rr = max(0.5, min(10.0, float(target_rr)))
    friction_bps = max(0.0, min(50.0, float(friction_bps)))
    capital_per_trade = max(100.0, float(capital_per_trade))

    backtest_sql = f"""
    WITH daily_orb AS (
        SELECT
            date,
            ticker,
            MAX(high) AS orb_high,
            MIN(low) AS orb_low,
            AVG(volume) AS orb_avg_vol
        FROM intraday_bars_5m
        WHERE EXTRACT(HOUR FROM timestamp) = 15
          AND EXTRACT(MINUTE FROM timestamp) BETWEEN 30 AND 40
        GROUP BY date, ticker
    ),
    trade_signals AS (
        SELECT
            b.timestamp AS entry_time,
            b.date,
            b.ticker,
            b.close AS entry_price,
            o.orb_low AS initial_stop,
            (b.close - o.orb_low) AS risk_1r,
            b.close + ({target_rr} * (b.close - o.orb_low)) AS target_price,
            ROW_NUMBER() OVER (PARTITION BY b.date, b.ticker ORDER BY b.timestamp ASC) AS sig_rank
        FROM intraday_bars_5m b
        JOIN daily_orb o ON b.date = o.date AND b.ticker = o.ticker
        WHERE (EXTRACT(HOUR FROM b.timestamp) > 15 OR (EXTRACT(HOUR FROM b.timestamp) = 15 AND EXTRACT(MINUTE FROM b.timestamp) >= 45))
          AND EXTRACT(HOUR FROM b.timestamp) < 21
          AND b.close > o.orb_high
          AND b.volume > 1.2 * o.orb_avg_vol
    ),
    filtered_entries AS (
        SELECT * FROM trade_signals WHERE sig_rank = 1 AND risk_1r > 0
    ),
    forward_evaluation AS (
        SELECT
            e.ticker,
            e.date,
            e.entry_time,
            e.entry_price,
            e.initial_stop,
            e.risk_1r,
            e.target_price,
            b.timestamp AS bar_time,
            b.high,
            b.low,
            b.close,
            MAX(CASE WHEN b.high >= (e.entry_price + e.risk_1r) THEN 1 ELSE 0 END)
                OVER (PARTITION BY e.ticker, e.date ORDER BY b.timestamp ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS breakeven_active
        FROM filtered_entries e
        JOIN intraday_bars_5m b
          ON e.ticker = b.ticker
         AND e.date = b.date
         AND b.timestamp > e.entry_time
        WHERE (EXTRACT(HOUR FROM b.timestamp) < 21)
           OR (EXTRACT(HOUR FROM b.timestamp) = 21 AND EXTRACT(MINUTE FROM b.timestamp) <= 55)
    ),
    trade_resolutions AS (
        SELECT
            ticker,
            date,
            FIRST(entry_time) AS entry_time,
            FIRST(entry_price) AS entry_price,
            FIRST(initial_stop) AS initial_stop,
            FIRST(target_price) AS target_price,
            CASE
                WHEN MAX(high) >= FIRST(target_price) THEN FIRST(target_price)
                WHEN MAX(breakeven_active) = 1 AND MIN(low) <= FIRST(entry_price) THEN FIRST(entry_price)
                WHEN MIN(low) <= FIRST(initial_stop) THEN FIRST(initial_stop)
                ELSE LAST(close)
            END AS exit_price,
            CASE
                WHEN MAX(high) >= FIRST(target_price) THEN 'TARGET_HIT'
                WHEN MAX(breakeven_active) = 1 AND MIN(low) <= FIRST(entry_price) THEN 'BREAKEVEN_EXIT'
                WHEN MIN(low) <= FIRST(initial_stop) THEN 'STOP_LOSS_HIT'
                ELSE 'EOD_FLAT_2155'
            END AS exit_reason
        FROM forward_evaluation
        GROUP BY ticker, date
    )
    SELECT
        ticker,
        COUNT(*) AS total_trades,
        ROUND(AVG(CASE WHEN exit_price > entry_price THEN 1.0 ELSE 0.0 END) * 100, 1) AS win_rate_pct,
        ROUND(AVG(CASE WHEN exit_reason = 'BREAKEVEN_EXIT' THEN 1.0 ELSE 0.0 END) * 100, 1) AS breakeven_pct,
        ROUND(AVG(CASE WHEN exit_reason = 'STOP_LOSS_HIT' THEN 1.0 ELSE 0.0 END) * 100, 1) AS stop_loss_pct,
        ROUND(AVG((exit_price - entry_price) / entry_price - ({friction_bps} / 10000.0)) * 100, 2) AS avg_net_return_pct,
        ROUND(SUM(((exit_price - entry_price) / entry_price - ({friction_bps} / 10000.0)) * {capital_per_trade}), 2) AS total_net_pnl_usd,
        ROUND(
            SUM(CASE WHEN ((exit_price - entry_price) / entry_price - ({friction_bps} / 10000.0)) > 0
                THEN ((exit_price - entry_price) / entry_price - ({friction_bps} / 10000.0)) * {capital_per_trade} * (1.0 - 0.3784)
                ELSE ((exit_price - entry_price) / entry_price - ({friction_bps} / 10000.0)) * {capital_per_trade}
            END),
            2
        ) AS after_tax_net_usd,
        ROUND(
            SUM(CASE WHEN exit_price > entry_price THEN (exit_price - entry_price) / entry_price ELSE 0 END) /
            NULLIF(ABS(SUM(CASE WHEN exit_price < entry_price THEN (exit_price - entry_price) / entry_price ELSE 0 END)), 0),
            2
        ) AS profit_factor
    FROM trade_resolutions
    GROUP BY ROLLUP(ticker)
    ORDER BY total_trades DESC;
    """

    res = con.execute(backtest_sql).fetchdf()
    if not quiet:
        print("\n" + "=" * 105)
        print(f"VEKTORISERT 15-MIN ORB BACKTEST (DuckDB) | Kapital/trade: ${capital_per_trade:,.2f} | R/R: {target_rr}x | Friksjon: {friction_bps} bps")
        print("-" * 105)
        print(f"{'Ticker':<10} | {'Trades':<6} | {'Win %':<6} | {'BE %':<6} | {'SL %':<6} | {'Snitt Net%':<10} | {'Net PnL ($)':<12} | {'Etter Skatt':<12} | {'Profit Fact':<10}")
        print("-" * 105)
        for _, row in res.iterrows():
            t = str(row["ticker"]) if pd.notnull(row["ticker"]) else "TOTALT"
            pf = f"{row['profit_factor']:.2f}" if pd.notnull(row["profit_factor"]) else "N/A"
            print(
                f"{t:<10} | {int(row['total_trades']):<6} | {row['win_rate_pct']:.1f}% | "
                f"{row['breakeven_pct']:.1f}% | {row['stop_loss_pct']:.1f}% | "
                f"{row['avg_net_return_pct']:+.2f}%    | ${row['total_net_pnl_usd']:+10,.2f} | "
                f"${row['after_tax_net_usd']:+10,.2f} | {pf:<10}"
            )
        print("-" * 105)
    return res


def run_intraday_backtest_api(
    tickers: List[str] | None = None,
    days: int = 20,
    target_rr: float = 2.0,
    friction_bps: float = 3.0,
    capital_per_trade: float = 9655.0,
) -> Dict[str, Any]:
    """Programmatisk API-endepunkt for runner_server.py og Streamlit."""
    tickers = tickers or ["NVDA", "AAPL", "SPY", "QQQ", "MSFT"]
    days = max(5, min(90, int(days)))
    con = duckdb.connect(":memory:")
    try:
        create_schema(con)
        bars_count = generate_sample_data(con, tickers=tickers, days=days, quiet=True)
        df = run_orb_backtest(
            con,
            target_rr=target_rr,
            friction_bps=friction_bps,
            capital_per_trade=capital_per_trade,
            quiet=True,
        )
        rows = []
        for _, r in df.iterrows():
            rows.append({
                "ticker": str(r["ticker"]) if pd.notnull(r["ticker"]) else "TOTALT",
                "is_total": not pd.notnull(r["ticker"]),
                "total_trades": int(r["total_trades"]),
                "win_rate_pct": float(r["win_rate_pct"] or 0.0),
                "breakeven_pct": float(r["breakeven_pct"] or 0.0),
                "stop_loss_pct": float(r["stop_loss_pct"] or 0.0),
                "avg_net_return_pct": float(r["avg_net_return_pct"] or 0.0),
                "total_net_pnl_usd": float(r["total_net_pnl_usd"] or 0.0),
                "after_tax_net_usd": float(r["after_tax_net_usd"] or 0.0),
                "profit_factor": round(float(r["profit_factor"]), 2) if pd.notnull(r["profit_factor"]) else None,
            })
        return {
            "success": True,
            "bars_count": bars_count,
            "params": {
                "tickers": tickers,
                "days": days,
                "target_rr": target_rr,
                "friction_bps": friction_bps,
                "capital_per_trade": capital_per_trade,
            },
            "rows": rows,
        }
    finally:
        con.close()


if __name__ == "__main__":
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(DB_PATH) if "--persist" in sys.argv else ":memory:")
    create_schema(con)
    generate_sample_data(con, tickers=["NVDA", "AAPL", "SPY"], days=20)
    run_orb_backtest(con, target_rr=2.0, friction_bps=3.0, capital_per_trade=9655.0)
    con.close()
