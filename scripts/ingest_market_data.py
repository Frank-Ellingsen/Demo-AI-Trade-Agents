"""Agent 2 CLI: Download 2019-2026 OHLCV & Macro data for OBX 15 and poll NewsWeb RSS."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.data_ingestion.newsweb_rss import poll_live_rss_feeds
from src.data_ingestion.ohlcv_loader import ingest_obx15_and_macro_to_duckdb
from src.db.duckdb_store import init_duckdb


def main() -> None:
    init_duckdb()
    print("[1/2] Downloading 2019-present OHLCV (raw + adj_close) & Macro series for OBX 15...")
    summary = ingest_obx15_and_macro_to_duckdb(start_date="2019-01-01")
    print(
        f"      Stocks requested        : {summary['stocks_requested']}\n"
        f"      OHLCV rows upserted     : {summary['ohlcv_rows_upserted']:,}\n"
        f"      Dividend events tracked : {summary['dividend_events_upserted']:,}\n"
        f"      Macro regime days built : {summary['macro_regime_days']:,}"
    )

    print("[2/2] Polling NewsWeb & financial RSS feeds for 72h OBX 15 notices...")
    news_count = poll_live_rss_feeds()
    print(f"      Matched OBX 15 notices  : {news_count}")


if __name__ == "__main__":
    main()
