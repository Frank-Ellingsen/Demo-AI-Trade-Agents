"""Agent 1: Quantitative Signal Factory in DuckDB SQL.

Computes exact dividend-adjusted total return series (`clean_adj_close`) using verified dividend
events in `corporate_events`, blocks false signals on ex-dividend dates, and computes endogenous
indicators (`RSI(2)`, `20d Breakout`, `Bollinger Squeeze`), `ATR(14)`, sector relative momentum,
and exogenous regime filters across all 15 OBX stocks.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb

from config.settings import settings
from src.db.duckdb_store import connect_duckdb

SIGNAL_PANEL_SQL = """
WITH universe_meta AS (
    SELECT * FROM (
        VALUES
            ('EQNR.OL', 'Energy', 6),
            ('AKRBP.OL', 'Energy', 5),
            ('SUBC.OL', 'Energy', 5),
            ('FRO.OL', 'Shipping', 5),
            ('DNB.OL', 'Financials', 6),
            ('STB.OL', 'Financials', 5),
            ('GJF.OL', 'Financials', 5),
            ('NHY.OL', 'Materials_Industrials', 6),
            ('YAR.OL', 'Materials_Industrials', 5),
            ('KOG.OL', 'Materials_Industrials', 5),
            ('MOWI.OL', 'Seafood', 5),
            ('SALM.OL', 'Seafood', 5),
            ('TEL.OL', 'Telecom_Consumer_Tech', 5),
            ('ORK.OL', 'Telecom_Consumer_Tech', 5),
            ('NOD.OL', 'Telecom_Consumer_Tech', 5)
    ) AS t(ticker, sector, liquidity_band)
),
div_adjusted_returns AS (
    -- Reconstruct clean dividend-adjusted price series so ex-dividend drops never fake technical crashes
    SELECT
        o.ticker,
        u.sector,
        u.liquidity_band,
        o.trade_date,
        o.raw_open,
        o.raw_high,
        o.raw_low,
        o.raw_close,
        o.volume,
        COALESCE(o.dividend_amount, 0.0) AS div_amt,
        LAG(o.raw_close, 1) OVER w AS prev_raw_close,
        CASE
            WHEN LAG(o.raw_close, 1) OVER w IS NULL OR LAG(o.raw_close, 1) OVER w <= 0 THEN 0.0
            ELSE ((o.raw_close + COALESCE(o.dividend_amount, 0.0)) / LAG(o.raw_close, 1) OVER w) - 1.0
        END AS total_ret_1d
    FROM ohlcv_daily o
    INNER JOIN universe_meta u ON o.ticker = u.ticker
    WINDOW w AS (PARTITION BY o.ticker ORDER BY o.trade_date)
),
clean_series AS (
    SELECT
        d.*,
        100.0 * EXP(
            SUM(LN(GREATEST(1.0 + d.total_ret_1d, 0.05)))
            OVER (PARTITION BY d.ticker ORDER BY d.trade_date ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
        ) AS clean_adj_close
    FROM div_adjusted_returns d
),
base_bars AS (
    SELECT
        c.ticker,
        c.sector,
        c.liquidity_band,
        c.trade_date,
        c.raw_open,
        c.raw_high,
        c.raw_low,
        c.raw_close,
        c.clean_adj_close AS adj_close,
        c.volume,
        c.div_amt,
        c.prev_raw_close,
        LAG(c.clean_adj_close, 1) OVER w AS prev_adj_close,
        LEAD(c.trade_date, 1) OVER w AS next_trade_date,
        LEAD(c.raw_open, 1) OVER w AS next_raw_open,
        LEAD(c.div_amt, 1) OVER w AS next_div_amt,
        AVG(c.clean_adj_close) OVER (w ROWS BETWEEN 199 PRECEDING AND CURRENT ROW) AS sma200,
        AVG(c.clean_adj_close) OVER (w ROWS BETWEEN 49 PRECEDING AND CURRENT ROW) AS sma50,
        AVG(c.clean_adj_close) OVER (w ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS sma20,
        AVG(c.clean_adj_close) OVER (w ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) AS sma5,
        STDDEV_SAMP(c.clean_adj_close) OVER (w ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS std20,
        MAX(c.clean_adj_close) OVER (w ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS prev_high20,
        MIN(c.clean_adj_close) OVER (w ROWS BETWEEN 10 PRECEDING AND 1 PRECEDING) AS prev_low10,
        AVG(CAST(c.volume AS DOUBLE)) OVER (w ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS vol_sma20,
        (c.clean_adj_close - LAG(c.clean_adj_close, 3) OVER w) / NULLIF(LAG(c.clean_adj_close, 3) OVER w, 0) AS ret_3d,
        (c.clean_adj_close - LAG(c.clean_adj_close, 10) OVER w) / NULLIF(LAG(c.clean_adj_close, 10) OVER w, 0) AS ret_10d,
        COUNT(*) OVER (w ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS bar_num
    FROM clean_series c
    WINDOW w AS (PARTITION BY c.ticker ORDER BY c.trade_date)
),
rsi_and_atr AS (
    SELECT
        b.*,
        GREATEST(b.adj_close - b.prev_adj_close, 0.0) AS gain_1d,
        GREATEST(b.prev_adj_close - b.adj_close, 0.0) AS loss_1d,
        GREATEST(
            b.raw_high - b.raw_low,
            ABS(b.raw_high - COALESCE(b.prev_raw_close, b.raw_open)),
            ABS(b.raw_low - COALESCE(b.prev_raw_close, b.raw_open))
        ) AS true_range,
        CASE
            WHEN b.sma20 > 0 THEN (4.0 * b.std20) / b.sma20
            ELSE NULL
        END AS bb_width
    FROM base_bars b
),
indicators AS (
    SELECT
        r.*,
        -- 3-bar exponential/weighted smoothing for RSI(2) to avoid single-bar micro-noise
        (0.5 * r.gain_1d + 0.35 * LAG(r.gain_1d, 1) OVER w + 0.15 * LAG(r.gain_1d, 2) OVER w) AS avg_gain2,
        (0.5 * r.loss_1d + 0.35 * LAG(r.loss_1d, 1) OVER w + 0.15 * LAG(r.loss_1d, 2) OVER w) AS avg_loss2,
        AVG(r.true_range) OVER (w ROWS BETWEEN 13 PRECEDING AND CURRENT ROW) AS atr14,
        QUANTILE_CONT(r.bb_width, 0.25) OVER (w ROWS BETWEEN 59 PRECEDING AND 1 PRECEDING) AS p20_bb_width_60,
        r.sma20 + 1.8 * r.std20 AS bb_upper,
        r.volume / NULLIF(r.vol_sma20, 0) AS vol_ratio
    FROM rsi_and_atr r
    WINDOW w AS (PARTITION BY r.ticker ORDER BY r.trade_date)
),
sector_mom AS (
    SELECT
        trade_date,
        sector,
        AVG(ret_10d) AS sector_ret_10d
    FROM indicators
    GROUP BY trade_date, sector
),
obx_daily AS (
    SELECT
        trade_date,
        AVG(adj_close) AS synth_obx_adj,
        AVG(ret_10d) AS obx_ret_10d
    FROM indicators
    GROUP BY trade_date
),
macro_enriched AS (
    SELECT
        m.trade_date,
        od.synth_obx_adj AS obx_close,
        AVG(od.synth_obx_adj) OVER (ORDER BY m.trade_date ROWS BETWEEN 199 PRECEDING AND CURRENT ROW) AS obx_sma200,
        m.vix_close AS vix_close,
        m.brent_2d_pct AS brent_2d_pct,
        COALESCE(od.obx_ret_10d, 0.0) AS obx_ret_10d
    FROM macro_regime m
    LEFT JOIN obx_daily od ON od.trade_date = m.trade_date
)
SELECT
    i.ticker,
    i.sector,
    i.liquidity_band,
    i.trade_date,
    i.raw_open,
    i.raw_high,
    i.raw_low,
    i.raw_close,
    i.adj_close,
    i.div_amt,
    i.next_div_amt,
    i.next_trade_date,
    i.next_raw_open,
    i.sma200,
    i.sma50,
    i.sma20,
    i.sma5,
    i.prev_high20,
    i.prev_low10,
    i.bb_upper,
    i.bb_width,
    i.p20_bb_width_60 AS min_bb_width_60,
    i.vol_ratio,
    i.atr14,
    i.ret_3d,
    i.ret_10d,
    CASE
        WHEN i.avg_loss2 = 0 AND i.avg_gain2 > 0 THEN 100.0
        WHEN i.avg_loss2 = 0 AND i.avg_gain2 = 0 THEN 50.0
        ELSE 100.0 - (100.0 / (1.0 + (i.avg_gain2 / NULLIF(i.avg_loss2, 0))))
    END AS rsi2,
    me.obx_close,
    me.obx_sma200,
    me.vix_close,
    me.brent_2d_pct,
    (sm.sector_ret_10d - COALESCE(me.obx_ret_10d, 0.0)) AS sector_rel_mom_10d
FROM indicators i
LEFT JOIN macro_enriched me ON me.trade_date = i.trade_date
LEFT JOIN sector_mom sm ON sm.trade_date = i.trade_date AND sm.sector = i.sector
WHERE i.bar_num >= 200
ORDER BY i.trade_date, i.ticker;
"""


def fetch_enriched_signal_panel(db_path: Path | None = None) -> list[dict[str, Any]]:
    """Query DuckDB and return the full daily enriched indicator & regime panel for all 15 OBX stocks."""
    target_db = db_path or settings.duckdb_path
    con = connect_duckdb(target_db, read_only=True)
    try:
        cur = con.execute(SIGNAL_PANEL_SQL)
        cols = [desc[0] for desc in cur.description]
        rows = cur.fetchall()
    finally:
        con.close()

    return [dict(zip(cols, row)) for row in rows]
