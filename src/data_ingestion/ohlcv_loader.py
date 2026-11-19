"""Agent 2: Historical & Daily OHLCV + Macro Regime Loader into DuckDB.

Fetches both unadjusted raw prices (raw_open, raw_high, raw_low, raw_close) for accurate order
execution & MiFID II tick-size simulation, and dividend-adjusted close (adj_close) for technical
indicators so ex-dividend drops never trigger false RSI(2) or breakout signals.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb
import yaml

from config.settings import settings

YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AntigravityOBX/0.1"


def load_universe_tickers(universe_yaml_path: Path | None = None) -> list[dict[str, Any]]:
    """Load the 15 OBX stock definitions from config/universe.yaml."""
    path = universe_yaml_path or (settings.base_dir / "config" / "universe.yaml")
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return list(data.get("stocks", []))


def fetch_yahoo_ohlcv(
    symbol: str,
    start_date: str = "2019-01-01",
    end_date: str | None = None,
    timeout: float = 15.0,
) -> tuple[list[tuple[Any, ...]], list[tuple[str, str, str, str, str]]]:
    """Fetch daily OHLCV (raw + adj_close) and dividend events from Yahoo Finance v8 Chart API.

    Returns:
        (ohlcv_rows, dividend_event_rows)
    """
    period1 = int(datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
    if end_date:
        period2 = int(datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
    else:
        period2 = int(datetime.now(timezone.utc).timestamp())

    encoded_sym = urllib.parse.quote(symbol)
    params = urllib.parse.urlencode(
        {
            "period1": period1,
            "period2": period2,
            "interval": "1d",
            "events": "div",
            "includeAdjustedClose": "true",
        }
    )
    url = f"{YAHOO_CHART_URL.format(symbol=encoded_sym)}?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    result_list = payload.get("chart", {}).get("result")
    if not result_list:
        return [], []

    res = result_list[0]
    timestamps = res.get("timestamp", [])
    indicators = res.get("indicators", {})
    quote_list = indicators.get("quote", [{}])
    quote = quote_list[0] if quote_list else {}
    adj_list = indicators.get("adjclose", [{}])
    adj_closes = adj_list[0].get("adjclose", []) if adj_list else []

    opens = quote.get("open", [])
    highs = quote.get("high", [])
    lows = quote.get("low", [])
    closes = quote.get("close", [])
    volumes = quote.get("volume", [])

    # Extract dividend map by date string YYYY-MM-DD
    div_events_raw = res.get("events", {}).get("dividends", {})
    div_by_date: dict[str, float] = {}
    corporate_div_rows: list[tuple[str, str, str, str, str]] = []
    for _, div_obj in div_events_raw.items():
        dt_str = datetime.fromtimestamp(div_obj["date"], tz=timezone.utc).strftime("%Y-%m-%d")
        amt = float(div_obj.get("amount", 0.0))
        div_by_date[dt_str] = amt
        corporate_div_rows.append(
            (symbol, "EX_DIVIDEND", dt_str, f"Ex-dividend {amt:.2f} NOK", "YahooChartEvents")
        )

    ohlcv_rows: list[tuple[Any, ...]] = []
    for idx, ts in enumerate(timestamps):
        raw_c = closes[idx] if idx < len(closes) else None
        raw_o = opens[idx] if idx < len(opens) else None
        raw_h = highs[idx] if idx < len(highs) else None
        raw_l = lows[idx] if idx < len(lows) else None
        vol = volumes[idx] if idx < len(volumes) else 0
        adj_c = adj_closes[idx] if idx < len(adj_closes) else raw_c

        if raw_c is None or raw_o is None or raw_h is None or raw_l is None:
            continue

        trade_dt = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        vol_int = int(vol or 0)
        raw_c_f = float(raw_c)
        adj_c_f = float(adj_c if adj_c is not None else raw_c_f)
        turnover_nok = round(raw_c_f * vol_int, 2)
        div_amt = div_by_date.get(trade_dt, 0.0)

        ohlcv_rows.append(
            (
                symbol,
                trade_dt,
                round(float(raw_o), 4),
                round(float(raw_h), 4),
                round(float(raw_l), 4),
                round(raw_c_f, 4),
                round(adj_c_f, 4),
                vol_int,
                turnover_nok,
                div_amt,
            )
        )

    return ohlcv_rows, corporate_div_rows


def ingest_obx15_and_macro_to_duckdb(
    start_date: str = "2019-01-01",
    db_path: Path | None = None,
    pause_seconds: float = 0.25,
) -> dict[str, int]:
    """Download OBX 15 stocks + macro indices (OBX, VIX, Brent, USDNOK) and store in DuckDB."""
    target_db = db_path or settings.duckdb_path
    stocks = load_universe_tickers()
    tickers = [s["ticker"] for s in stocks]

    # Include Oslo Børs benchmark & macro series
    macro_symbols = {
        "OBX": "OBX.OL",
        "OSEBX_FALLBACK": "^OSEAX",
        "VIX": "^VIX",
        "BRENT": "BZ=F",
        "USDNOK": "NOK=X",
    }

    total_ohlcv_rows = 0
    total_div_rows = 0

    con = duckdb.connect(str(target_db))
    try:
        for sym in tickers:
            try:
                rows, div_rows = fetch_yahoo_ohlcv(sym, start_date=start_date)
            except Exception as exc:
                print(f"[WARN] Failed to fetch {sym}: {exc}")
                continue

            if rows:
                con.executemany(
                    """
                    INSERT OR REPLACE INTO ohlcv_daily (
                        ticker, trade_date, raw_open, raw_high, raw_low,
                        raw_close, adj_close, volume, turnover_nok, dividend_amount
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    rows,
                )
                total_ohlcv_rows += len(rows)

            if div_rows:
                con.executemany(
                    """
                    INSERT OR REPLACE INTO corporate_events (
                        ticker, event_type, event_date, headline, source
                    ) VALUES (?, ?, ?, ?, ?);
                    """,
                    div_rows,
                )
                total_div_rows += len(div_rows)

            time.sleep(pause_seconds)

        # Fetch macro series into ohlcv_daily as reference series and build macro_regime table
        for label, m_sym in macro_symbols.items():
            try:
                m_rows, _ = fetch_yahoo_ohlcv(m_sym, start_date=start_date)
                if m_rows:
                    con.executemany(
                        """
                        INSERT OR REPLACE INTO ohlcv_daily (
                            ticker, trade_date, raw_open, raw_high, raw_low,
                            raw_close, adj_close, volume, turnover_nok, dividend_amount
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                        """,
                        m_rows,
                    )
            except Exception:
                pass
            time.sleep(pause_seconds)

        rebuild_macro_regime_table(con)
        macro_count = con.execute("SELECT COUNT(*) FROM macro_regime").fetchone()[0]
    finally:
        con.close()

    return {
        "stocks_requested": len(tickers),
        "ohlcv_rows_upserted": total_ohlcv_rows,
        "dividend_events_upserted": total_div_rows,
        "macro_regime_days": int(macro_count),
    }


def rebuild_macro_regime_table(con: duckdb.DuckDBPyConnection) -> None:
    """Populate `macro_regime` using DuckDB window functions (OBX SMA200, VIX, Brent 2d % change).

    If `OBX.OL` has sparse history on Yahoo, synthesizes an equal-weighted index from the 15 OBX
    stocks as a seamless fallback so `obx_close` and `obx_sma200` are always populated.
    """
    con.execute(
        """
        CREATE OR REPLACE TEMP VIEW synthetic_obx AS
        SELECT
            trade_date,
            AVG(adj_close) AS synth_obx_close
        FROM ohlcv_daily
        WHERE ticker LIKE '%.OL' AND ticker != 'OBX.OL'
        GROUP BY trade_date;

        CREATE OR REPLACE TEMP VIEW obx_base AS
        SELECT
            s.trade_date,
            COALESCE(o.adj_close, s.synth_obx_close) AS obx_close
        FROM synthetic_obx s
        LEFT JOIN ohlcv_daily o
          ON o.ticker = 'OBX.OL' AND o.trade_date = s.trade_date;

        CREATE OR REPLACE TEMP VIEW brent_series AS
        SELECT
            trade_date,
            raw_close AS brent_close,
            (raw_close - LAG(raw_close, 2) OVER (ORDER BY trade_date))
                / NULLIF(LAG(raw_close, 2) OVER (ORDER BY trade_date), 0) AS brent_2d_pct
        FROM ohlcv_daily
        WHERE ticker = 'BZ=F';

        INSERT OR REPLACE INTO macro_regime (
            trade_date, obx_close, obx_sma200, vix_close, brent_close, brent_2d_pct, usdnok
        )
        SELECT
            b.trade_date,
            ROUND(b.obx_close, 4) AS obx_close,
            ROUND(
                AVG(b.obx_close) OVER (
                    ORDER BY b.trade_date
                    ROWS BETWEEN 199 PRECEDING AND CURRENT ROW
                ),
                4
            ) AS obx_sma200,
            v.raw_close AS vix_close,
            br.brent_close,
            ROUND(br.brent_2d_pct, 6) AS brent_2d_pct,
            fx.raw_close AS usdnok
        FROM obx_base b
        LEFT JOIN ohlcv_daily v
          ON v.ticker = '^VIX' AND v.trade_date = b.trade_date
        LEFT JOIN brent_series br
          ON br.trade_date = b.trade_date
        LEFT JOIN ohlcv_daily fx
          ON fx.ticker = 'NOK=X' AND fx.trade_date = b.trade_date
        ORDER BY b.trade_date;
        """
    )
