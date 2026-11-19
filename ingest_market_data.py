"""OBX 15 Market Data Ingestion & Schema Pipeline (`ingest_market_data.py`).

1. Downloads historical daily OHLCV + dividend events for the 15 OBX stocks, OBX proxy, Brent, VIX, and USD/NOK
   using our dependency-free Yahoo v8 Chart loader (`src.data_ingestion.ohlcv_loader`) with optional `yfinance` support.
2. Populates both the canonical `ohlcv_daily` / `macro_regime` tables AND the compatibility tables/views:
   - `prices_daily`: OHLCV + adj_close + turnover_nok per stock
   - `macro_daily`: obx_close, brent_close, vix_close, usdnok_close
   - `sector_daily`: Daily equal-weighted sector index (base 100.0) for relative momentum
3. Runs `setup_drift_monitoring_views` so all Agent 5 drift views are immediately ready.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import duckdb
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config.settings import settings
from setup_drift_views import setup_drift_monitoring_views
from src.data_ingestion.newsweb_rss import poll_live_rss_feeds
from src.data_ingestion.ohlcv_loader import ingest_obx15_and_macro_to_duckdb
from src.db.duckdb_store import connect_duckdb, init_duckdb

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("DataIngestion")

OBX_15_SECTORS = {
    "EQNR.OL": "Energi",
    "AKRBP.OL": "Energi",
    "SUBC.OL": "Energi",
    "FRO.OL": "Shipping",
    "DNB.OL": "Finans",
    "STB.OL": "Finans",
    "GJF.OL": "Finans",
    "NHY.OL": "Materialer",
    "YAR.OL": "Materialer",
    "KOG.OL": "Industri",
    "MOWI.OL": "Sjømat",
    "SALM.OL": "Sjømat",
    "TEL.OL": "Telekom",
    "ORK.OL": "Konsum",
    "NOD.OL": "Teknologi",
}


def compute_and_insert_sectors(con: duckdb.DuckDBPyConnection) -> None:
    """Compute equal-weighted daily sector indices (base 100.0) in DuckDB `sector_daily`."""
    logger.info("Beregner likevektede sektorindekser i DuckDB (sector_daily)...")
    con.execute(
        """
        CREATE OR REPLACE TABLE sector_daily AS
        WITH stock_returns AS (
            SELECT
                date,
                ticker,
                sector,
                adj_close,
                (adj_close / NULLIF(LAG(adj_close) OVER (PARTITION BY ticker ORDER BY date), 0) - 1.0) AS daily_ret
            FROM prices_daily
        ),
        sector_agg AS (
            SELECT
                date,
                sector,
                AVG(daily_ret) AS avg_sector_ret
            FROM stock_returns
            WHERE daily_ret IS NOT NULL
            GROUP BY date, sector
        )
        SELECT
            date,
            sector,
            EXP(SUM(LN(1.0 + GREATEST(avg_sector_ret, -0.99))) OVER (PARTITION BY sector ORDER BY date)) * 100.0 AS sector_index
        FROM sector_agg;
        """
    )


def run_pipeline(
    db_path: str | Path = "data/obx_analytics.duckdb",
    start_date: str = "2019-01-01",
    refresh_remote: bool = False,
) -> pd.DataFrame:
    """Orchestrate market data ingestion, sector index computation, and drift views in DuckDB."""
    target_path = Path(db_path)
    init_duckdb(target_path)

    con = connect_duckdb(target_path, read_only=True)
    try:
        existing_rows = con.execute("SELECT COUNT(*) FROM ohlcv_daily").fetchone()[0]
    finally:
        con.close()

    if refresh_remote or existing_rows == 0:
        logger.info(f"Henter historiske OHLCV- og makroserier fra {start_date}...")
        ingest_obx15_and_macro_to_duckdb(start_date=start_date, db_path=target_path)
        poll_live_rss_feeds(db_path=target_path)

    setup_drift_monitoring_views(target_path)

    con = connect_duckdb(target_path, read_only=False)
    try:
        compute_and_insert_sectors(con)
        summary = con.execute(
            """
            SELECT
                ticker,
                sector,
                MIN(date) AS forste_dato,
                MAX(date) AS siste_dato,
                COUNT(*) AS antall_dager,
                ROUND(AVG(turnover_nok) / 1000000, 1) AS snitt_omsetning_mnok,
                ROUND(AVG(adj_close), 2) AS snitt_kurs
            FROM prices_daily
            GROUP BY ticker, sector
            ORDER BY sector, ticker;
            """
        ).df()
    finally:
        con.close()

    print("\n" + "=" * 95)
    print("OBX 15 DUCKDB INGESTION STATUS SUMMARY")
    print("=" * 95)
    print(summary.to_string(index=False))
    print("=" * 95 + "\n")
    return summary


if __name__ == "__main__":
    start = sys.argv[1] if len(sys.argv) > 1 else "2019-01-01"
    target_db = sys.argv[2] if len(sys.argv) > 2 else str(settings.duckdb_path)
    run_pipeline(db_path=target_db, start_date=start, refresh_remote=("--refresh" in sys.argv))
