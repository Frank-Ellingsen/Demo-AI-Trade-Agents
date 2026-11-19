"""Unit tests for Agent 2 (The Intelligence / Data Scraper: NewsWeb RSS, Earnings Calendar, Macro DB)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import duckdb

from src.data_ingestion.corporate_actions import (
    get_days_until_next_earnings,
    is_in_earnings_blackout,
    upsert_earnings_date,
)
from src.data_ingestion.newsweb_rss import (
    build_72h_news_digest_for_ticker,
    match_obx15_ticker,
    parse_rss_xml,
    save_news_messages_to_duckdb,
)
from src.data_ingestion.ohlcv_loader import rebuild_macro_regime_table
from src.db.duckdb_store import init_duckdb

SAMPLE_NEWSWEB_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Oslo Børs NewsWeb</title>
    <item>
      <title>Equinor ASA: Mandatory notification of trade - share buyback</title>
      <description>Equinor has purchased own shares under tranche 3 of the 2026 programme.</description>
      <link>https://newsweb.oslobors.no/message/600001</link>
      <category>MELDEPLIKTIG HANDEL</category>
      <pubDate>Tue, 17 Nov 2026 08:15:00 +0100</pubDate>
    </item>
    <item>
      <title>Kongsberg Gruppen signs defence contract worth NOK 2.4 billion</title>
      <description>Kongsberg Defence &amp; Aerospace has signed a new NSM missile contract.</description>
      <link>https://newsweb.oslobors.no/message/600002</link>
      <category>AVTALER</category>
      <pubDate>Tue, 17 Nov 2026 10:30:00 +0100</pubDate>
    </item>
    <item>
      <title>Unrelated SmallCap AS: Notice of EGF</title>
      <description>Notice of extraordinary general meeting.</description>
      <link>https://newsweb.oslobors.no/message/600003</link>
      <pubDate>Tue, 17 Nov 2026 11:00:00 +0100</pubDate>
    </item>
  </channel>
</rss>
"""


def test_obx15_ticker_matching_and_rss_parsing(tmp_path: Path) -> None:
    """Verify RSS XML parser extracts only OBX 15 notices and formats 72h digest for Agent 3."""
    duck_file = tmp_path / "test_agent2.duckdb"
    init_duckdb(duck_file)

    assert match_obx15_ticker("Equinor ASA: Share buyback") == "EQNR.OL"
    assert match_obx15_ticker("Kongsberg Gruppen Q3 results") == "KOG.OL"
    assert match_obx15_ticker("Random SmallCap AS") is None

    parsed = parse_rss_xml(SAMPLE_NEWSWEB_RSS, source_category="NewsWeb")
    assert len(parsed) == 2
    assert {m.ticker for m in parsed} == {"EQNR.OL", "KOG.OL"}

    saved_count = save_news_messages_to_duckdb(parsed, db_path=duck_file)
    assert saved_count == 2

    ref_dt = datetime(2026, 11, 17, 16, 40, tzinfo=timezone.utc)
    eqnr_digest = build_72h_news_digest_for_ticker(
        ticker="EQNR.OL",
        lookback_hours=72,
        reference_time=ref_dt,
        db_path=duck_file,
    )
    assert "Equinor ASA: Mandatory notification of trade" in eqnr_digest
    assert "MELDEPLIKTIG HANDEL" in eqnr_digest


def test_corporate_actions_earnings_guardrail(tmp_path: Path) -> None:
    """Verify 3-day Earnings Guardrail calendar lookup in DuckDB."""
    duck_file = tmp_path / "test_earnings.duckdb"
    init_duckdb(duck_file)

    upsert_earnings_date(
        ticker="MOWI.OL",
        earnings_date="2026-11-19",
        headline="Q3 2026 Quarterly Report",
        db_path=duck_file,
    )
    days = get_days_until_next_earnings(
        ticker="MOWI.OL",
        as_of_date=date(2026, 11, 17),
        db_path=duck_file,
    )
    assert days == 2

    blacked_out, rem_days = is_in_earnings_blackout(
        ticker="MOWI.OL",
        as_of_date=date(2026, 11, 17),
        blackout_days=3,
        db_path=duck_file,
    )
    assert blacked_out is True
    assert rem_days == 2


def test_macro_regime_window_builder(tmp_path: Path) -> None:
    """Verify DuckDB macro_regime window computation (OBX SMA200 & Brent 2d % change)."""
    duck_file = tmp_path / "test_macro.duckdb"
    init_duckdb(duck_file)

    con = duckdb.connect(str(duck_file))
    try:
        # Insert 3 days of synthetic EQNR.OL, ^VIX, and BZ=F (Brent Crude)
        sample_rows = [
            ("EQNR.OL", "2026-11-13", 280.0, 282.0, 278.0, 280.0, 280.0, 1000000, 2.8e8, 0.0),
            ("EQNR.OL", "2026-11-16", 282.0, 285.0, 281.0, 284.0, 284.0, 1100000, 3.1e8, 0.0),
            ("EQNR.OL", "2026-11-17", 285.0, 290.0, 284.0, 288.0, 288.0, 1200000, 3.4e8, 0.0),
            ("BZ=F", "2026-11-13", 75.0, 76.0, 74.0, 75.0, 75.0, 100000, 0.0, 0.0),
            ("BZ=F", "2026-11-16", 75.5, 76.5, 75.0, 76.0, 76.0, 100000, 0.0, 0.0),
            ("BZ=F", "2026-11-17", 76.0, 78.0, 75.8, 78.0, 78.0, 100000, 0.0, 0.0),
            ("^VIX", "2026-11-17", 16.0, 16.5, 15.5, 15.8, 15.8, 0, 0.0, 0.0),
        ]
        con.executemany(
            """
            INSERT INTO ohlcv_daily (
                ticker, trade_date, raw_open, raw_high, raw_low,
                raw_close, adj_close, volume, turnover_nok, dividend_amount
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            sample_rows,
        )
        rebuild_macro_regime_table(con)
        row = con.execute(
            "SELECT obx_close, vix_close, brent_close, brent_2d_pct FROM macro_regime WHERE trade_date = '2026-11-17'"
        ).fetchone()
    finally:
        con.close()

    assert row is not None
    assert row[0] == 288.0
    assert row[1] == 15.8
    assert row[2] == 78.0
    # Brent moved from 75.0 (Nov 13) to 78.0 (Nov 17) = +4.0% (+0.04)
    assert row[3] == 0.04
