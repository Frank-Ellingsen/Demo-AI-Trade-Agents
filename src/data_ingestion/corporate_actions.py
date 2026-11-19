"""Agent 2: Corporate Actions & Quarterly Earnings Blackout Calendar.

Provides deterministic 3-trading-day Earnings Guardrail lookups against DuckDB `corporate_events`
so Agent 3 (AI Risk Officer) and Agent 1 (Quant Engine) never enter positions right before
quarterly earnings releases.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import duckdb

from config.settings import settings


def upsert_earnings_date(
    ticker: str,
    earnings_date: date | str,
    headline: str = "Quarterly Earnings Release",
    source: str = "Calendar",
    db_path: Path | None = None,
) -> None:
    """Register or update an upcoming quarterly earnings release date in DuckDB."""
    target_db = db_path or settings.duckdb_path
    dt_val = date.fromisoformat(earnings_date) if isinstance(earnings_date, str) else earnings_date
    con = duckdb.connect(str(target_db))
    try:
        con.execute(
            """
            INSERT OR REPLACE INTO corporate_events (
                ticker, event_type, event_date, headline, source
            ) VALUES (?, 'EARNINGS', ?, ?, ?);
            """,
            (ticker, dt_val, headline, source),
        )
    finally:
        con.close()


def get_days_until_next_earnings(
    ticker: str,
    as_of_date: date | None = None,
    db_path: Path | None = None,
) -> int | None:
    """Return calendar/trading days until the next scheduled EARNINGS event for `ticker`."""
    target_db = db_path or settings.duckdb_path
    if not target_db.exists():
        return None

    ref_date = as_of_date or date.today()
    con = duckdb.connect(str(target_db), read_only=True)
    try:
        row = con.execute(
            """
            SELECT MIN(event_date)
            FROM corporate_events
            WHERE ticker = ?
              AND event_type = 'EARNINGS'
              AND event_date >= ?;
            """,
            (ticker, ref_date),
        ).fetchone()
    finally:
        con.close()

    if not row or row[0] is None:
        return None

    next_dt: date = row[0]
    return (next_dt - ref_date).days


def is_in_earnings_blackout(
    ticker: str,
    as_of_date: date | None = None,
    blackout_days: int = 3,
    db_path: Path | None = None,
) -> tuple[bool, int | None]:
    """Return (is_blacked_out, days_until_earnings) using the 3-day Earnings Guardrail."""
    days = get_days_until_next_earnings(ticker=ticker, as_of_date=as_of_date, db_path=db_path)
    if days is not None and 0 <= days <= blackout_days:
        return True, days
    return False, days
