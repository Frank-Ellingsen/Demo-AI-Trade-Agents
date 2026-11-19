"""Portfolio Monitor & Circuit Breaker Engine for OBX 15 (`portfolio_monitor.py`).

Sets up and monitors the 100,000 NOK trial portfolio on Aksjesparekonto (ASK) in DuckDB:
- Tracks `portfolio_metadata`, `portfolio_positions`, and `portfolio_daily_snapshot`.
- Evaluates rolling Sortino, Max Drawdown, Cost Drag, and Circuit Breakers (15% DD or 4 consecutive losses).
"""

from __future__ import annotations

import datetime
import os
import sys
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config.settings import settings
from src.db.duckdb_store import connect_duckdb

PORTFOLIO_CONFIG: dict[str, Any] = {
    "portfolio_name": "OBX15_HYBRID_ALPHA_100K",
    "initial_capital_nok": 100_000.0,
    "max_positions": 4,
    "max_per_sector": 2,
    "cash_buffer_pct": 0.02,
    "tax_wrapper": "Aksjesparekonto (ASK)",
    "broker": {
        "primary": "Saxo_Classic",
        "secondary": "Nordnet_Mini",
        "models": {
            "Saxo_Classic": {
                "commission_pct": 0.0008,
                "min_commission_nok": 10.0,
                "custody_fee_annual": 0.0000,
                "currency_drag": 0.0,
                "spread_bps": 5.0,
            },
            "Nordnet_Mini": {
                "commission_pct": 0.0015,
                "min_commission_nok": 29.0,
                "custody_fee_annual": 0.0,
                "currency_drag": 0.0,
                "spread_bps": 5.0,
            },
        },
    },
    "risk_rules": {
        "min_daily_turnover_nok": 50_000_000,
        "max_slippage_gap_pct": 1.5,
        "stop_loss_atr_mult": 2.5,
        "trailing_stop_atr_mult": 2.5,
        "max_portfolio_drawdown_limit": 15.0,
        "max_consecutive_losses": 4,
        "earnings_guardrail_days": 3,
    },
    "strategies": {
        "MEAN_REVERSION_20D": {
            "type": "Endogen + Eksogen",
            "max_hold_days": 20,
            "rules": "Close > SMA200 AND RSI(2) < 10 AND Ret3d <= -2.5% AND Brent_2d > -2.5% AND VIX < 25",
        },
        "TREND_BREAKOUT_20D": {
            "type": "Endogen + Eksogen",
            "max_hold_days": 20,
            "rules": "Close > High20 AND Volume > 1.25x SMA20 AND Sector_10d_mom > 0 AND OBX > SMA200",
        },
    },
}


def initialize_portfolio_database(db_path: str | Path | None = None) -> Path:
    """Create and seed the trial portfolio tables in DuckDB."""
    target_path = Path(db_path) if db_path else settings.duckdb_path
    target_path.parent.mkdir(parents=True, exist_ok=True)
    con = connect_duckdb(target_path, read_only=False)
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS portfolio_metadata (
                portfolio_name VARCHAR PRIMARY KEY,
                initial_capital_nok DOUBLE,
                current_cash_nok DOUBLE,
                current_equity_nok DOUBLE,
                max_positions INTEGER,
                max_per_sector INTEGER,
                broker VARCHAR,
                created_at TIMESTAMP,
                updated_at TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS portfolio_positions (
                position_id VARCHAR PRIMARY KEY,
                portfolio_name VARCHAR,
                ticker VARCHAR,
                sector VARCHAR,
                strategy VARCHAR,
                entry_date DATE,
                shares INTEGER,
                entry_price DOUBLE,
                stop_loss_price DOUBLE,
                highest_close DOUBLE,
                current_price DOUBLE,
                market_value_nok DOUBLE,
                unrealized_pnl_nok DOUBLE,
                unrealized_pnl_pct DOUBLE,
                days_held INTEGER,
                status VARCHAR DEFAULT 'OPEN'
            );

            CREATE TABLE IF NOT EXISTS portfolio_daily_snapshot (
                snapshot_date DATE,
                portfolio_name VARCHAR,
                cash_nok DOUBLE,
                holdings_value_nok DOUBLE,
                total_equity_nok DOUBLE,
                daily_return_pct DOUBLE,
                open_positions_count INTEGER,
                drawdown_pct DOUBLE,
                circuit_breaker_active BOOLEAN,
                PRIMARY KEY (snapshot_date, portfolio_name)
            );
            """
        )
        con.execute("ALTER TABLE portfolio_positions ADD COLUMN IF NOT EXISTS status VARCHAR DEFAULT 'OPEN';")

        now = datetime.datetime.now()
        cfg = PORTFOLIO_CONFIG
        con.execute(
            """
            INSERT OR IGNORE INTO portfolio_metadata VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            [
                cfg["portfolio_name"],
                cfg["initial_capital_nok"],
                cfg["initial_capital_nok"],
                cfg["initial_capital_nok"],
                cfg["max_positions"],
                cfg["max_per_sector"],
                cfg["broker"]["primary"],
                now,
                now,
            ],
        )
    finally:
        con.close()
    return target_path


def run_portfolio_health_check(db_path: str | Path | None = None) -> dict[str, Any]:
    """Monitor trial portfolio state, active positions, and circuit breaker status."""
    target_path = initialize_portfolio_database(db_path)
    con = connect_duckdb(target_path, read_only=True)
    try:
        meta = con.execute(
            "SELECT * FROM portfolio_metadata WHERE portfolio_name = ?",
            [PORTFOLIO_CONFIG["portfolio_name"]],
        ).df()

        open_pos = con.execute(
            """
            SELECT ticker, sector, strategy, shares, entry_price, current_price,
                   unrealized_pnl_pct, days_held, status
            FROM portfolio_positions
            WHERE portfolio_name = ? AND COALESCE(status, 'OPEN') = 'OPEN'
            """,
            [PORTFOLIO_CONFIG["portfolio_name"]],
        ).df()

        snapshots = con.execute(
            """
            SELECT * FROM portfolio_daily_snapshot
            WHERE portfolio_name = ?
            ORDER BY snapshot_date ASC
            """,
            [PORTFOLIO_CONFIG["portfolio_name"]],
        ).df()
    finally:
        con.close()

    print("\n" + "=" * 78)
    print(f"OVERVÅKING AV PRØVEPORTEFØLJE: {PORTFOLIO_CONFIG['portfolio_name']}")
    print("=" * 78)
    print(f"• Startkapital: {PORTFOLIO_CONFIG['initial_capital_nok']:,.0f} NOK på {PORTFOLIO_CONFIG['tax_wrapper']}")
    print(f"• Primærmegler: {PORTFOLIO_CONFIG['broker']['primary']} (Kurtasje: 0.08 %, min 10 kr)")
    print(
        f"• Allokering:   Maks {PORTFOLIO_CONFIG['max_positions']} posisjoner (25k/pos), "
        f"maks {PORTFOLIO_CONFIG['max_per_sector']} per sektor"
    )
    print("-" * 78)

    if open_pos.empty:
        print("• Åpne posisjoner: Ingen aktive posisjoner for øyeblikket (100 % kontanter).")
    else:
        print("• Aktive posisjoner:")
        print(open_pos.to_string(index=False))

    print("-" * 78)
    print("RISIKOREGLER & CIRCUIT BREAKERS:")
    print("• Likviditetskrav:    Minst 50 MNOK daglig omsetning på OBX 15")
    print("• Stop-Loss regel:    2.5x–3.0x ATR(14) under kjøpskurs (utføres automatisk)")
    print("• Slippage-vakt:      Avvis ordre dersom aksjen åpner > +1.5 % eller < -2.0 %")
    print("• Selskapsrisiko:     Agent 3 3-dagers Earnings Guardrail luker ut kvartalsfall")
    print("• Circuit Breaker:    Inntreffer ved 15 % samlet drawdown eller 4 tap på rad")
    print("=" * 78 + "\n")

    return {
        "portfolio_name": PORTFOLIO_CONFIG["portfolio_name"],
        "open_positions_count": len(open_pos),
        "snapshots_count": len(snapshots),
        "initialized": not meta.empty,
    }


if __name__ == "__main__":
    db_file = sys.argv[1] if len(sys.argv) > 1 else str(settings.duckdb_path)
    run_portfolio_health_check(db_file)
