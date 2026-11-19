"""DuckDB analytical store for Agents 1, 2, 3 (signal_evaluations), and 5 (trade_ledger)."""

from __future__ import annotations

import time
from pathlib import Path
import duckdb

from config.settings import settings

DUCKDB_SCHEMA_DDL = """
-- 1. Agent 1 & 2: Daily OHLCV with raw execution prices and dividend-adjusted indicator prices
CREATE TABLE IF NOT EXISTS ohlcv_daily (
    ticker VARCHAR NOT NULL,
    trade_date DATE NOT NULL,
    raw_open DOUBLE NOT NULL,
    raw_high DOUBLE NOT NULL,
    raw_low DOUBLE NOT NULL,
    raw_close DOUBLE NOT NULL,
    adj_close DOUBLE NOT NULL,
    volume BIGINT NOT NULL,
    turnover_nok DOUBLE,
    dividend_amount DOUBLE DEFAULT 0.0,
    PRIMARY KEY (ticker, trade_date)
);

-- 2. Agent 1: Exogenous market & commodity regime series
CREATE TABLE IF NOT EXISTS macro_regime (
    trade_date DATE PRIMARY KEY,
    obx_close DOUBLE NOT NULL,
    obx_sma200 DOUBLE,
    vix_close DOUBLE,
    brent_close DOUBLE,
    brent_2d_pct DOUBLE,
    usdnok DOUBLE
);

-- 3. Agent 4: Euronext Oslo Børs MiFID II Tick-Size lookup table
CREATE TABLE IF NOT EXISTS tick_size_rules (
    liquidity_band INTEGER NOT NULL,
    min_price DOUBLE NOT NULL,
    max_price DOUBLE NOT NULL,
    tick_size DOUBLE NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (liquidity_band, min_price)
);

-- 4. Agent 2: Corporate actions (Quarterly earnings & ex-dividend calendar)
CREATE TABLE IF NOT EXISTS corporate_events (
    ticker VARCHAR NOT NULL,
    event_type VARCHAR NOT NULL, -- 'EARNINGS', 'EX_DIVIDEND'
    event_date DATE NOT NULL,
    headline VARCHAR,
    source VARCHAR DEFAULT 'NewsWeb',
    PRIMARY KEY (ticker, event_type, event_date)
);

-- 5. Agent 2: NewsWeb 48-72 hour rolling archive for LLM context injection
CREATE TABLE IF NOT EXISTS newsweb_messages (
    message_id VARCHAR PRIMARY KEY,
    ticker VARCHAR NOT NULL,
    published_at TIMESTAMP NOT NULL,
    category VARCHAR,
    title VARCHAR NOT NULL,
    summary TEXT,
    url VARCHAR
);

-- 6. Agent 3: AI Risk Manager Evaluations (Logs ALL GO and NO-GO decisions for Shadow Tracking)
CREATE TABLE IF NOT EXISTS signal_evaluations (
    eval_id VARCHAR PRIMARY KEY,
    signal_date DATE NOT NULL,
    ticker VARCHAR NOT NULL,
    strategy_name VARCHAR NOT NULL,
    horizon_days INTEGER NOT NULL,
    signal_close_price DOUBLE NOT NULL,
    atr_stop_loss DOUBLE NOT NULL,
    llm_provider VARCHAR NOT NULL,
    approve_recommendation BOOLEAN NOT NULL,
    rejection_category VARCHAR DEFAULT 'NONE', -- 'NONE', 'EARNINGS_SOON', 'COMPANY_SHOCK', 'MACRO_NOISE'
    confidence DOUBLE,
    risk_score VARCHAR NOT NULL,
    earnings_blackout_flag BOOLEAN NOT NULL,
    bull_case TEXT,
    devils_advocate TEXT,
    evaluated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 7. Agent 5: Backtest & Shadow Tracking Trade Ledger (Cohorts A, B, and C)
CREATE TABLE IF NOT EXISTS trade_ledger (
    trade_id VARCHAR PRIMARY KEY,
    eval_id VARCHAR,                  -- Links to signal_evaluations.eval_id
    cohort VARCHAR NOT NULL,          -- 'COHORT_A_BASE', 'COHORT_B_LLM_GO', 'COHORT_C_LLM_NOGO'
    broker_profile VARCHAR NOT NULL,  -- 'saxo_classic' or 'nordnet_mini'
    strategy_name VARCHAR NOT NULL,
    horizon_window INTEGER NOT NULL,  -- 10, 20, or 30
    ticker VARCHAR NOT NULL,
    sector VARCHAR NOT NULL,
    signal_date DATE NOT NULL,
    entry_date DATE NOT NULL,
    exit_date DATE,
    shares INTEGER NOT NULL,
    entry_raw_price DOUBLE NOT NULL,
    exit_raw_price DOUBLE,
    gross_pnl_nok DOUBLE,
    commission_nok DOUBLE,
    slippage_spread_nok DOUBLE,
    custody_fee_nok DOUBLE DEFAULT 0.0,
    net_pnl_nok DOUBLE,
    net_return_pct DOUBLE,
    obx_benchmark_return_pct DOUBLE,
    sector_rel_return_pct DOUBLE,
    pure_strategy_alpha_pct DOUBLE,
    exit_reason VARCHAR               -- 'SIGNAL_EXIT', 'STOP_LOSS', 'TIME_STOP', 'CANCELLED_GAP'
);
"""

DEFAULT_TICK_SIZE_ROWS = [
    # Liquidity Band 5 (most OBX 15 stocks)
    (5, 0.0, 10.0, 0.005),
    (5, 10.0, 20.0, 0.01),
    (5, 20.0, 50.0, 0.02),
    (5, 50.0, 100.0, 0.05),
    (5, 100.0, 200.0, 0.10),
    (5, 200.0, 500.0, 0.20),
    (5, 500.0, 1000.0, 0.50),
    (5, 1000.0, 50000.0, 1.00),
    # Liquidity Band 6 (EQNR, DNB, NHY ultra-liquid tier)
    (6, 0.0, 10.0, 0.002),
    (6, 10.0, 20.0, 0.005),
    (6, 20.0, 50.0, 0.01),
    (6, 50.0, 100.0, 0.02),
    (6, 100.0, 200.0, 0.05),
    (6, 200.0, 500.0, 0.10),
    (6, 500.0, 1000.0, 0.20),
    (6, 1000.0, 50000.0, 0.50),
]


def connect_duckdb(db_path: Path | str | None = None, read_only: bool = False, retries: int = 20) -> duckdb.DuckDBPyConnection:
    """Connect to DuckDB with automatic retry on transient Windows file locks."""
    target_path = Path(db_path) if db_path is not None else settings.duckdb_path
    target_path.parent.mkdir(parents=True, exist_ok=True)
    last_err: Exception | None = None
    for _ in range(retries):
        try:
            return duckdb.connect(str(target_path), read_only=read_only)
        except Exception as e:
            last_err = e
            time.sleep(0.25)
    raise last_err  # type: ignore[misc]


def init_duckdb(db_path: Path | None = None) -> Path:
    """Create the DuckDB analytical database and seed default Euronext MiFID II tick sizes."""
    target_path = db_path or settings.duckdb_path
    con = connect_duckdb(target_path, read_only=False)
    try:
        con.execute(DUCKDB_SCHEMA_DDL)
        con.execute("ALTER TABLE signal_evaluations ADD COLUMN IF NOT EXISTS rejection_category VARCHAR DEFAULT 'NONE';")
        con.executemany(
            """
            INSERT OR REPLACE INTO tick_size_rules (liquidity_band, min_price, max_price, tick_size)
            VALUES (?, ?, ?, ?);
            """,
            DEFAULT_TICK_SIZE_ROWS,
        )
    finally:
        con.close()
    return target_path
