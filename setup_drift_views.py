"""Setup DuckDB Drift Monitoring Views & Compatibility Tables (`setup_drift_views.py`).

Creates and verifies:
1. Compatibility views (`prices_daily`, `macro_daily`, `sector_daily`, `backtest_trades`)
   bridged seamlessly to `ohlcv_daily`, `macro_regime`, and `trade_ledger`.
2. `v_rolling_strategy_drift_60d`: Rolling 60-day concept-drift monitor (Sortino, Cost Drag %, Pure Alpha).
3. `v_gemini_shadow_tracking_audit`: Cohort A (All) vs. Cohort B (AI GO) vs. Cohort C (AI NO-GO) forward 10d outcomes.
4. `v_gemini_rejection_reasons`: Paranoia vs. Value-Add audit by rejection reason (`EARNINGS_SOON`, `COMPANY_SHOCK`, `MACRO_NOISE`).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config.settings import settings
from src.db.duckdb_store import connect_duckdb, init_duckdb

DEFAULT_DUCKDB_PATH = str(settings.duckdb_path)


def _drop_any_relation(con: duckdb.DuckDBPyConnection, name: str) -> None:
    """Safely drop either a VIEW or a TABLE in DuckDB."""
    try:
        con.execute(f"DROP VIEW IF EXISTS {name} CASCADE;")
    except Exception:
        pass
    try:
        con.execute(f"DROP TABLE IF EXISTS {name} CASCADE;")
    except Exception:
        pass


def setup_drift_monitoring_views(db_path: str | Path = DEFAULT_DUCKDB_PATH) -> None:
    """Create and verify all drift monitoring tables, compatibility views, and Shadow Tracking SQL views in DuckDB."""
    target_path = Path(db_path)
    init_duckdb(target_path)

    con = connect_duckdb(target_path, read_only=False)
    try:
        # Ensure signal_evaluations has both core and alias columns
        con.execute("ALTER TABLE signal_evaluations ADD COLUMN IF NOT EXISTS rejection_category VARCHAR DEFAULT 'NONE';")
        con.execute("ALTER TABLE signal_evaluations ADD COLUMN IF NOT EXISTS user_decision VARCHAR DEFAULT 'PENDING';")

        # Bridge `prices_daily` over `ohlcv_daily` (if `ohlcv_daily` is populated)
        ohlcv_cnt = con.execute("SELECT COUNT(*) FROM ohlcv_daily").fetchone()[0]
        if ohlcv_cnt > 0:
            _drop_any_relation(con, "prices_daily")
            con.execute(
                """
                CREATE OR REPLACE VIEW prices_daily AS
                SELECT
                    trade_date AS date,
                    ticker,
                    CASE
                        WHEN ticker IN ('EQNR.OL', 'AKRBP.OL', 'SUBC.OL') THEN 'Energi'
                        WHEN ticker = 'FRO.OL' THEN 'Shipping'
                        WHEN ticker IN ('DNB.OL', 'STB.OL', 'GJF.OL', 'GJFG.OL') THEN 'Finans'
                        WHEN ticker IN ('NHY.OL', 'YAR.OL') THEN 'Materialer'
                        WHEN ticker = 'KOG.OL' THEN 'Industri'
                        WHEN ticker IN ('MOWI.OL', 'SALM.OL') THEN 'Sjømat'
                        WHEN ticker = 'TEL.OL' THEN 'Telekom'
                        WHEN ticker = 'ORK.OL' THEN 'Konsum'
                        WHEN ticker = 'NOD.OL' THEN 'Teknologi'
                        ELSE 'Annet'
                    END AS sector,
                    raw_open AS open,
                    raw_high AS high,
                    raw_low AS low,
                    raw_close AS close,
                    adj_close,
                    volume,
                    turnover_nok
                FROM ohlcv_daily
                WHERE ticker LIKE '%.OL' AND ticker != 'OBX.OL';
                """
            )
        else:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS prices_daily (
                    date DATE NOT NULL,
                    ticker VARCHAR NOT NULL,
                    sector VARCHAR DEFAULT 'OBX',
                    open DOUBLE,
                    high DOUBLE,
                    low DOUBLE,
                    close DOUBLE,
                    adj_close DOUBLE,
                    volume BIGINT,
                    turnover_nok DOUBLE,
                    PRIMARY KEY (date, ticker)
                );
                """
            )

        # Bridge `macro_daily` over `macro_regime`
        macro_cnt = con.execute("SELECT COUNT(*) FROM macro_regime").fetchone()[0]
        if macro_cnt > 0:
            _drop_any_relation(con, "macro_daily")
            con.execute(
                """
                CREATE OR REPLACE VIEW macro_daily AS
                SELECT
                    trade_date AS date,
                    obx_close,
                    brent_close,
                    vix_close,
                    usdnok AS usdnok_close
                FROM macro_regime;
                """
            )
        else:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS macro_daily (
                    date DATE PRIMARY KEY,
                    obx_close DOUBLE,
                    brent_close DOUBLE,
                    vix_close DOUBLE,
                    usdnok_close DOUBLE
                );
                """
            )

        # Bridge `backtest_trades` over `trade_ledger`
        ledger_cnt = con.execute("SELECT COUNT(*) FROM trade_ledger").fetchone()[0]
        if ledger_cnt > 0:
            _drop_any_relation(con, "backtest_trades")
            con.execute(
                """
                CREATE OR REPLACE VIEW backtest_trades AS
                SELECT
                    strategy_name || '_' || CAST(horizon_window AS VARCHAR) || 'D_' || UPPER(broker_profile) AS case_id,
                    trade_id,
                    ticker,
                    sector,
                    strategy_name AS strategy,
                    entry_date,
                    COALESCE(exit_date, entry_date) AS exit_date,
                    GREATEST(1, CAST(COALESCE(exit_date, entry_date) - entry_date AS INTEGER)) AS days_held,
                    shares,
                    entry_raw_price AS entry_price,
                    COALESCE(exit_raw_price, entry_raw_price) AS exit_price,
                    (COALESCE(commission_nok, 0.0) + COALESCE(slippage_spread_nok, 0.0) + COALESCE(custody_fee_nok, 0.0)) AS fees_nok,
                    COALESCE(net_pnl_nok, 0.0) AS net_pnl_nok,
                    COALESCE(net_return_pct, 0.0) * 100.0 AS net_ret_pct,
                    COALESCE(obx_benchmark_return_pct, 0.0) * 100.0 AS market_contrib_pct,
                    COALESCE(sector_rel_return_pct, 0.0) * 100.0 AS sector_contrib_pct
                FROM trade_ledger;
                """
            )
        else:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS backtest_trades (
                    case_id VARCHAR NOT NULL,
                    trade_id VARCHAR PRIMARY KEY,
                    ticker VARCHAR NOT NULL,
                    sector VARCHAR NOT NULL,
                    strategy VARCHAR NOT NULL,
                    entry_date DATE NOT NULL,
                    exit_date DATE NOT NULL,
                    days_held INTEGER NOT NULL,
                    shares INTEGER NOT NULL,
                    entry_price DOUBLE NOT NULL,
                    exit_price DOUBLE NOT NULL,
                    fees_nok DOUBLE NOT NULL,
                    net_pnl_nok DOUBLE NOT NULL,
                    net_ret_pct DOUBLE NOT NULL,
                    market_contrib_pct DOUBLE,
                    sector_contrib_pct DOUBLE
                );
                """
            )

        # 1. Rolling 60-day Strategy Performance & Concept Drift View
        con.execute(
            """
            CREATE OR REPLACE VIEW v_rolling_strategy_drift_60d AS
            WITH recent_trades AS (
                SELECT
                    case_id,
                    ticker,
                    exit_date,
                    net_ret_pct,
                    COALESCE(market_contrib_pct, 0.0) AS mkt_contrib,
                    COALESCE(sector_contrib_pct, 0.0) AS sec_contrib,
                    (net_ret_pct - COALESCE(market_contrib_pct, 0.0) - COALESCE(sector_contrib_pct, 0.0)) AS pure_alpha_pct,
                    fees_nok,
                    net_pnl_nok
                FROM backtest_trades
                WHERE exit_date >= (SELECT COALESCE(MAX(exit_date), CURRENT_DATE) - INTERVAL 60 DAYS FROM backtest_trades)
            ),
            metrics AS (
                SELECT
                    case_id,
                    COUNT(*) AS n_trades_60d,
                    ROUND(AVG(CASE WHEN net_ret_pct > 0 THEN 1.0 ELSE 0.0 END) * 100, 1) AS win_rate_60d,
                    ROUND(AVG(net_ret_pct), 2) AS avg_net_ret_pct,
                    ROUND(AVG(pure_alpha_pct), 2) AS avg_alpha_60d,
                    ROUND((AVG(net_ret_pct) / NULLIF(STDDEV_SAMP(CASE WHEN net_ret_pct < 0 THEN net_ret_pct END), 0)) * SQRT(25.2), 2) AS sortino_60d,
                    ROUND((SUM(fees_nok) / NULLIF(SUM(CASE WHEN (net_pnl_nok + fees_nok) > 0 THEN (net_pnl_nok + fees_nok) ELSE 0 END), 0)) * 100, 1) AS cost_drag_60d
                FROM recent_trades
                GROUP BY case_id
            )
            SELECT
                case_id,
                n_trades_60d,
                win_rate_60d,
                avg_net_ret_pct,
                avg_alpha_60d,
                COALESCE(sortino_60d, 0.0) AS sortino_60d,
                COALESCE(cost_drag_60d, 0.0) AS cost_drag_60d,
                CASE
                    WHEN COALESCE(sortino_60d, 0.0) < 1.0 OR avg_alpha_60d <= 0.0 THEN '🔴 KONSEPTDRIFT_VARSEL'
                    WHEN COALESCE(cost_drag_60d, 0.0) > 20.0 THEN '🟡 HOY_KOSTNADSFRIKSJON'
                    ELSE '🟢 SUNN_STRATEGI'
                END AS drift_status
            FROM metrics;
            """
        )

        # 2. AI Risk Officer A/B Shadow Tracking Audit View
        con.execute(
            """
            CREATE OR REPLACE VIEW v_gemini_shadow_tracking_audit AS
            WITH forward_window AS (
                SELECT
                    s.eval_id AS signal_id,
                    s.signal_date,
                    s.ticker,
                    s.approve_recommendation AS gemini_approve,
                    COALESCE(s.rejection_category, s.devils_advocate) AS devils_advocate_reason,
                    s.signal_close_price AS entry_ref_price,
                    s.atr_stop_loss AS stop_loss_price,
                    p.date AS trade_date,
                    p.low,
                    p.close,
                    ROW_NUMBER() OVER (PARTITION BY s.eval_id ORDER BY p.date ASC) AS hold_day
                FROM signal_evaluations s
                JOIN prices_daily p
                  ON s.ticker = p.ticker
                 AND p.date > s.signal_date
            ),
            trade_outcomes AS (
                SELECT
                    signal_id,
                    MAX(CASE WHEN hold_day <= 10 AND low <= stop_loss_price THEN 1 ELSE 0 END) AS hit_stop_loss,
                    MAX(CASE WHEN hold_day = 10 THEN close END) AS exit_close_10d,
                    FIRST(entry_ref_price) AS entry_price,
                    FIRST(stop_loss_price) AS stop_price,
                    FIRST(gemini_approve) AS gemini_approve,
                    FIRST(devils_advocate_reason) AS reason
                FROM forward_window
                WHERE hold_day <= 10
                GROUP BY signal_id
            ),
            net_returns AS (
                SELECT
                    *,
                    CASE
                        WHEN hit_stop_loss = 1 THEN ((stop_price - entry_price) / NULLIF(entry_price, 0)) - 0.0025
                        ELSE ((exit_close_10d - entry_price) / NULLIF(entry_price, 0)) - 0.0025
                    END AS net_ret
                FROM trade_outcomes
                WHERE exit_close_10d IS NOT NULL
            )
            SELECT
                CASE
                    WHEN GROUPING(gemini_approve) = 1 THEN '1. ALLE (Baseline)'
                    WHEN gemini_approve = TRUE THEN '2. AI GO (Kohort B)'
                    ELSE '3. AI NO-GO (Kohort C)'
                END AS cohort,
                COUNT(*) AS n_signaler,
                ROUND(AVG(CASE WHEN net_ret > 0 THEN 1.0 ELSE 0.0 END) * 100, 1) AS win_rate_pct,
                ROUND(AVG(net_ret) * 100, 2) AS avg_net_ret_pct,
                ROUND(AVG(hit_stop_loss) * 100, 1) AS stop_loss_pct,
                ROUND((AVG(net_ret) / NULLIF(STDDEV_SAMP(net_ret), 0)) * SQRT(25.2), 2) AS annualized_sharpe
            FROM net_returns
            GROUP BY ROLLUP(gemini_approve)
            ORDER BY cohort;
            """
        )

        # 3. Rejection Reason Breakdown View (`EARNINGS_SOON` vs `COMPANY_SHOCK` vs `MACRO_NOISE`)
        con.execute(
            """
            CREATE OR REPLACE VIEW v_gemini_rejection_reasons AS
            WITH forward_window AS (
                SELECT
                    s.eval_id AS signal_id,
                    COALESCE(s.rejection_category, s.devils_advocate, 'UKJENT') AS devils_advocate_reason,
                    s.signal_close_price AS entry_ref_price,
                    s.atr_stop_loss AS stop_loss_price,
                    p.low,
                    p.close,
                    ROW_NUMBER() OVER (PARTITION BY s.eval_id ORDER BY p.date ASC) AS hold_day
                FROM signal_evaluations s
                JOIN prices_daily p
                  ON s.ticker = p.ticker
                 AND p.date > s.signal_date
                WHERE s.approve_recommendation = FALSE
            ),
            outcomes AS (
                SELECT
                    signal_id,
                    FIRST(devils_advocate_reason) AS reason,
                    MAX(CASE WHEN hold_day <= 10 AND low <= stop_loss_price THEN 1 ELSE 0 END) AS hit_sl,
                    MAX(CASE WHEN hold_day = 10 THEN close END) AS exit_10d,
                    FIRST(entry_ref_price) AS entry_p,
                    FIRST(stop_loss_price) AS stop_p
                FROM forward_window
                WHERE hold_day <= 10
                GROUP BY signal_id
            )
            SELECT
                reason,
                COUNT(*) AS n_avviste,
                ROUND(AVG(CASE WHEN hit_sl = 1 THEN (stop_p - entry_p)/NULLIF(entry_p, 0) - 0.0025 ELSE (exit_10d - entry_p)/NULLIF(entry_p, 0) - 0.0025 END) * 100, 2) AS hypotetisk_snitt_ret_pct,
                ROUND(AVG(hit_sl) * 100, 1) AS stop_loss_treff_pct,
                CASE
                    WHEN AVG(CASE WHEN hit_sl = 1 THEN (stop_p - entry_p)/NULLIF(entry_p, 0) - 0.0025 ELSE (exit_10d - entry_p)/NULLIF(entry_p, 0) - 0.0025 END) > 0.015
                    THEN '⚠️ PARANOIA (Blokkerer vinnere)'
                    ELSE '🛡️ VERDIFULL FILTERING'
                END AS filter_vurdering
            FROM outcomes
            WHERE exit_10d IS NOT NULL
            GROUP BY reason
            ORDER BY n_avviste DESC;
            """
        )
    finally:
        con.close()


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DUCKDB_PATH
    setup_drift_monitoring_views(target)
    print(f"[OK] Drift-monitoring tabeller og views er opprettet og verifisert i {target}!")
