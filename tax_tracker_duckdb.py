#!/usr/bin/env python3
"""
tax_tracker_duckdb.py
=============================================================================
Automatisk skatteberegningsmodul og skattejournal i DuckDB.
Sporer akkumulert realisert og urealisert skatteplikt for handler i Alpaca (US)
og Saxo Bank (utenfor ASK), i henhold til gjeldende norske skatteregler (37.84%).
Beregner nøyaktig skatteavsetning (Tax Drag), USD/NOK valutaeffekt og W-8BEN kredit.
=============================================================================
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import os
import duckdb

BASE_DIR = Path(__file__).resolve().parent
_RUNTIME_DIR = Path(os.getenv("LOCALAPPDATA") or str(BASE_DIR / "data")) / "ai_trader_runtime"
_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = _RUNTIME_DIR / "tax_tracking.duckdb"


class NorwegianTaxTracker:
    # 22% nominell alminnelig inntektssats * 1.72 oppjusteringsfaktor = 37.84%
    EFFECTIVE_EQUITY_TAX_RATE = 0.3784
    US_WHT_TREATY_RATE = 0.15  # W-8BEN sats på utbytte/renter fra USA

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = Path(db_path) if db_path else DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = duckdb.connect(str(self.db_path))
        self._init_tables()

    def _init_tables(self) -> None:
        """Oppretter skattetabeller for lukkede posisjoner, utbytter og årlig skatteoppgjør."""
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS tax_realized_trades (
                trade_id VARCHAR PRIMARY KEY,
                broker VARCHAR NOT NULL,
                account_type VARCHAR NOT NULL,
                ticker VARCHAR NOT NULL,
                asset_class VARCHAR NOT NULL,
                currency VARCHAR NOT NULL,
                open_date TIMESTAMP NOT NULL,
                close_date TIMESTAMP NOT NULL,
                tax_year INTEGER NOT NULL,
                fx_rate_open DOUBLE NOT NULL,
                fx_rate_close DOUBLE NOT NULL,
                cost_basis_ccy DOUBLE NOT NULL,
                proceeds_gross_ccy DOUBLE NOT NULL,
                fees_commissions_ccy DOUBLE NOT NULL,
                net_profit_ccy DOUBLE NOT NULL,
                cost_basis_nok DOUBLE NOT NULL,
                proceeds_net_nok DOUBLE NOT NULL,
                taxable_gain_nok DOUBLE NOT NULL,
                effective_tax_rate DOUBLE NOT NULL,
                tax_due_nok DOUBLE NOT NULL,
                notes TEXT
            );

            CREATE TABLE IF NOT EXISTS tax_dividend_income (
                div_id VARCHAR PRIMARY KEY,
                broker VARCHAR NOT NULL,
                ticker VARCHAR NOT NULL,
                pay_date TIMESTAMP NOT NULL,
                tax_year INTEGER NOT NULL,
                gross_div_ccy DOUBLE NOT NULL,
                fx_rate DOUBLE NOT NULL,
                us_wht_paid_ccy DOUBLE NOT NULL,
                gross_div_nok DOUBLE NOT NULL,
                norwegian_tax_gross_nok DOUBLE NOT NULL,
                foreign_tax_credit_nok DOUBLE NOT NULL,
                net_norwegian_tax_due_nok DOUBLE NOT NULL
            );
        """)

    def record_closed_trade(
        self,
        trade_id: str,
        broker: str,
        ticker: str,
        asset_class: str,
        currency: str,
        open_date: str,
        close_date: str,
        cost_basis_ccy: float,
        proceeds_gross_ccy: float,
        fees_commissions_ccy: float,
        fx_open: float = 10.95,
        fx_close: float = 11.00,
        account_type: str = "TAXABLE_CASH",
    ) -> Dict[str, Any]:
        """Registrerer en lukket handel og regner ut skattekravet i NOK inkl. FX-effekt."""
        c_date = datetime.fromisoformat(close_date)
        tax_year = c_date.year

        net_profit_ccy = proceeds_gross_ccy - cost_basis_ccy - fees_commissions_ccy
        cost_basis_nok = cost_basis_ccy * fx_open
        proceeds_net_nok = (proceeds_gross_ccy - fees_commissions_ccy) * fx_close
        taxable_gain_nok = proceeds_net_nok - cost_basis_nok
        tax_due_nok = taxable_gain_nok * self.EFFECTIVE_EQUITY_TAX_RATE

        params = [
            trade_id, broker, account_type, ticker.upper(), asset_class, currency.upper(),
            open_date, close_date, tax_year,
            fx_open, fx_close,
            cost_basis_ccy, proceeds_gross_ccy, fees_commissions_ccy, net_profit_ccy,
            cost_basis_nok, proceeds_net_nok, taxable_gain_nok,
            self.EFFECTIVE_EQUITY_TAX_RATE, tax_due_nok,
            f"Beregnet med 37.84% sats (fx_open={fx_open}, fx_close={fx_close})",
        ]
        placeholders = ", ".join(["?"] * len(params))
        self.conn.execute(f"INSERT OR REPLACE INTO tax_realized_trades VALUES ({placeholders})", params)

        return {
            "trade_id": trade_id,
            "ticker": ticker.upper(),
            "broker": broker,
            "net_profit_ccy": round(net_profit_ccy, 2),
            "taxable_gain_nok": round(taxable_gain_nok, 2),
            "tax_due_nok": round(tax_due_nok, 2),
            "status": "TAX_LIABILITY_RECORDED" if tax_due_nok > 0 else "TAX_DEDUCTION_RECORDED",
        }

    def get_recent_trades(self, tax_year: int = 2026, limit: int = 25) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT trade_id, broker, ticker, asset_class, currency, CAST(close_date AS VARCHAR),
                   fx_rate_open, fx_rate_close, net_profit_ccy, taxable_gain_nok, tax_due_nok
            FROM tax_realized_trades
            WHERE tax_year = ?
            ORDER BY close_date DESC
            LIMIT ?
            """,
            [tax_year, limit],
        ).fetchall()
        cols = [
            "trade_id", "broker", "ticker", "asset_class", "currency", "close_date",
            "fx_rate_open", "fx_rate_close", "net_profit_ccy", "taxable_gain_nok", "tax_due_nok",
        ]
        return [dict(zip(cols, r)) for r in rows]

    def get_annual_tax_summary(self, tax_year: int = 2026) -> Dict[str, Any]:
        """Henter samlet skatteregnskap og nødvendig skatteavsetning for gitt inntektsår."""
        res = self.conn.execute(
            """
            SELECT
                COUNT(*) as total_trades,
                COALESCE(SUM(CASE WHEN taxable_gain_nok > 0 THEN 1 ELSE 0 END), 0) as winning_trades,
                COALESCE(SUM(CASE WHEN taxable_gain_nok < 0 THEN 1 ELSE 0 END), 0) as losing_trades,
                COALESCE(SUM(taxable_gain_nok), 0.0) as total_taxable_gain_nok,
                COALESCE(SUM(tax_due_nok), 0.0) as total_tax_due_nok,
                COALESCE(SUM(fees_commissions_ccy), 0.0) as total_friction_ccy
            FROM tax_realized_trades
            WHERE tax_year = ?
            """,
            [tax_year],
        ).fetchone()

        by_broker = self.conn.execute(
            """
            SELECT broker,
                   COUNT(*) as trades,
                   ROUND(SUM(taxable_gain_nok), 2) as gain_nok,
                   ROUND(SUM(tax_due_nok), 2) as tax_nok
            FROM tax_realized_trades
            WHERE tax_year = ?
            GROUP BY broker
            """,
            [tax_year],
        ).fetchall()

        return {
            "tax_year": tax_year,
            "effective_tax_rate": "37.84%",
            "total_trades": int(res[0] or 0),
            "win_trades": int(res[1] or 0),
            "loss_trades": int(res[2] or 0),
            "net_taxable_gain_nok": round(float(res[3] or 0.0), 2),
            "accumulated_tax_liability_nok": round(float(res[4] or 0.0), 2),
            "recommended_cash_reserve_nok": max(0.0, round(float(res[4] or 0.0), 2)),
            "breakdown_by_broker": [
                {"broker": b[0], "trades": int(b[1]), "gain_nok": float(b[2]), "tax_nok": float(b[3])}
                for b in by_broker
            ],
            "recent_trades": self.get_recent_trades(tax_year=tax_year, limit=20),
        }

    def close(self) -> None:
        self.conn.close()


if __name__ == "__main__":
    tracker = NorwegianTaxTracker(str(BASE_DIR / "data" / "tax_tracking_test.duckdb"))
    tracker.record_closed_trade("TR-ALP-001", "Alpaca", "NVDA", "EQUITY", "USD", "2026-11-20T15:45:00", "2026-11-20T21:45:00", 4620.0, 5508.0, 1.50, 10.95, 11.02)
    tracker.record_closed_trade("TR-SAXO-002", "Saxo_Standard", "TSLA", "OPTION_PUT", "USD", "2026-11-21T16:00:00", "2026-11-21T20:30:00", 4640.0, 5780.0, 15.0, 11.00, 11.05)
    tracker.record_closed_trade("TR-ALP-003", "Alpaca", "SPY", "EQUITY", "USD", "2026-11-22T15:35:00", "2026-11-22T16:15:00", 4500.0, 4320.0, 1.20, 11.05, 11.04)
    print(tracker.get_annual_tax_summary(2026))
    tracker.close()
