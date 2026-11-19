"""Daily Scan & Orchestration Script for OBX Trading System (`run_daily_scan.py`).

Executed automatically after market close (16:40 CET):
1. Verifies DuckDB (`prices_daily`, `ohlcv_daily`) and SQLite WAL (`hitl_orders.db`) schemas.
2. Expires stale PENDING orders (> 14h old).
3. Scans the 15 OBX stocks for endogenous triggers (`rsi_mean_reversion`, `trend_breakout`)
   and exogenous macro filters (`OBX > SMA200`, `VIX < 25`, `Brent 2d`, and `> 50 MNOK` liquidity).
4. Checks portfolio capacity against `portfolio_positions` (max 4 concurrent, max 2 per sector).
5. Runs Agent 3 (Hybrid Ollama / Gemini Risk Officer) & Agent 4 (MiFID II tick-size + HITL queue).
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config.settings import settings
from portfolio_monitor import initialize_portfolio_database
from setup_drift_views import setup_drift_monitoring_views
from src.backtest.engine import evaluate_entry_trigger
from src.data_ingestion.corporate_actions import get_days_until_next_earnings
from src.data_ingestion.newsweb_rss import build_72h_news_digest_for_ticker
from src.db.duckdb_store import connect_duckdb
from src.db.sqlite_store import get_sqlite_conn, init_sqlite_db
from src.hitl.llm_validator import evaluate_signal_with_risk_officer
from src.hitl.order_queue import enqueue_order_proposal, expire_stale_orders, get_reserved_cash_nok
from src.portfolio.allocator import CandidateSignal, select_portfolio_candidates
from src.portfolio.cost_tax_model import round_to_valid_tick
from src.signals.exogenous import ExogenousRegimeState, check_exogenous_regime
from src.signals.factory import fetch_enriched_signal_panel

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("DailyScan")

DUCKDB_PATH = os.environ.get("DUCKDB_PATH", str(settings.duckdb_path))
HITL_DB_PATH = os.environ.get("HITL_DB_PATH", str(settings.sqlite_db_path))


def run_daily_scan(
    duckdb_path: str | Path = DUCKDB_PATH,
    hitl_db_path: str | Path = HITL_DB_PATH,
) -> dict[str, Any]:
    """Run the full 5-agent daily EOD scan and return a structured summary."""
    logger.info("Starter daglig markedsskanning for OBX 15...")
    target_duck = Path(duckdb_path)
    target_sqlite = Path(hitl_db_path)

    if not target_duck.exists():
        logger.error(f"Fant ikke DuckDB-database på {target_duck}. Kjør ingest_market_data.py først.")
        return {"status": "ERROR_NO_DB", "queued_orders": []}

    init_sqlite_db(target_sqlite)
    initialize_portfolio_database(target_duck)
    setup_drift_monitoring_views(target_duck)

    expired_cnt = expire_stale_orders(db_path=target_sqlite)
    if expired_cnt:
        logger.info(f"[Agent 4] Utløpte {expired_cnt} ubesvarte ordrer (> 14 timer).")

    con = connect_duckdb(target_duck, read_only=True)
    try:
        latest_date = con.execute("SELECT MAX(date) FROM prices_daily").fetchone()[0]
        open_positions = con.execute(
            "SELECT COUNT(*) FROM portfolio_positions WHERE COALESCE(status, 'OPEN') = 'OPEN'"
        ).fetchone()[0]
        sec_rows = con.execute(
            """
            SELECT sector, COUNT(*)
            FROM portfolio_positions
            WHERE COALESCE(status, 'OPEN') = 'OPEN'
            GROUP BY sector
            """
        ).fetchall()
        active_by_sector = {str(r[0]): int(r[1]) for r in sec_rows}
        open_tickers = {
            str(r[0])
            for r in con.execute(
                "SELECT ticker FROM portfolio_positions WHERE COALESCE(status, 'OPEN') = 'OPEN'"
            ).fetchall()
        }

        # Check liquidity (> 50 MNOK 20-day average turnover) and primary trend (Close > SMA200)
        trend_liquid_rows = con.execute(
            """
            WITH technicals AS (
                SELECT
                    ticker,
                    date,
                    close,
                    adj_close,
                    AVG(adj_close) OVER(PARTITION BY ticker ORDER BY date ROWS BETWEEN 199 PRECEDING AND CURRENT ROW) AS sma_200,
                    AVG(turnover_nok) OVER(PARTITION BY ticker ORDER BY date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS adv_20d_nok
                FROM prices_daily
                WHERE date <= ?
            )
            SELECT ticker, close, sma_200, adv_20d_nok
            FROM technicals
            WHERE date = ?
              AND adj_close > sma_200
              AND adv_20d_nok >= 50000000;
            """,
            [latest_date, latest_date],
        ).fetchall()
    finally:
        con.close()

    panel = fetch_enriched_signal_panel(db_path=target_duck)
    latest_rows = [r for r in panel if str(r["trade_date"]) == str(latest_date)]
    sector_by_ticker = {str(r["ticker"]): str(r["sector"]) for r in latest_rows}

    reserved_tickers: set[str] = set()
    with get_sqlite_conn(target_sqlite) as sqlite_conn:
        reserved_rows = sqlite_conn.execute(
            """
            SELECT DISTINCT ticker
            FROM pending_orders
            WHERE action = 'BUY'
              AND status IN ('PENDING', 'APPROVED')
            """
        ).fetchall()
        reserved_tickers = {
            str(row["ticker"])
            for row in reserved_rows
            if str(row["ticker"]) not in open_tickers
        }

    active_reserved_positions = 0
    for ticker in reserved_tickers:
        sector = sector_by_ticker.get(ticker)
        if ticker and sector:
            active_reserved_positions += 1
            active_by_sector[sector] = int(active_by_sector.get(sector, 0)) + 1

    effective_open_positions = int(open_positions) + active_reserved_positions
    available_slots = max(0, settings.max_concurrent_positions - effective_open_positions)
    slot_budget_nok = settings.initial_capital_nok / settings.max_concurrent_positions
    reserved_cash_nok = get_reserved_cash_nok(target_sqlite)
    available_cash_nok = max(
        0.0,
        settings.initial_capital_nok - (open_positions * slot_budget_nok) - reserved_cash_nok,
    )
    logger.info(
        f"Siste handelsdag: {latest_date} | Likvide aksjer i opptrend (>50 MNOK): {len(trend_liquid_rows)} "
        f"| Åpne posisjoner: {open_positions} | Reserverte ventende/approved: {active_reserved_positions} "
        f"| Ledige plasser: {available_slots} | Reservert kontant: {reserved_cash_nok:.2f} NOK"
    )

    raw_candidates: list[CandidateSignal] = []
    row_by_ticker: dict[str, dict[str, Any]] = {}

    for r in latest_rows:
        row_by_ticker[r["ticker"]] = r
        vix_close = r["vix_close"]
        brent_2d_pct = r["brent_2d_pct"]
        if vix_close is None or brent_2d_pct is None:
            logger.warning(
                "[Agent 1] Mangler makrofilter for %s; signalet er blokkert.",
                r["ticker"],
            )
            continue
        reg = ExogenousRegimeState(
            obx_close=float(r["obx_close"] or 0.0),
            obx_sma200=float(r["obx_sma200"] or 0.0),
            vix_close=float(vix_close),
            brent_2d_pct=float(brent_2d_pct),
            sector_rel_momentum_10d=float(r["sector_rel_mom_10d"] or 0.0),
        )
        ok_reg, _ = check_exogenous_regime(reg, sector=r["sector"])
        if not ok_reg:
            continue

        for strat in ("rsi_mean_reversion", "trend_breakout"):
            triggered, rank, _ = evaluate_entry_trigger(r, strat)
            if triggered:
                raw_candidates.append(
                    CandidateSignal(
                        ticker=r["ticker"],
                        sector=r["sector"],
                        strategy_name=strat,
                        signal_close=float(r["raw_close"]),
                        priority_rank=rank,
                    )
                )

    selected = select_portfolio_candidates(
        candidates=raw_candidates,
        active_positions_by_sector=active_by_sector,
        current_open_positions=effective_open_positions,
        max_total_positions=settings.max_concurrent_positions,
        max_per_sector=settings.max_positions_per_sector,
        available_cash_nok=available_cash_nok,
        position_notional_nok=slot_budget_nok,
    )

    queued_orders: list[dict[str, Any]] = []

    for cand in selected:
        r = row_by_ticker[cand.ticker]
        news_72h = build_72h_news_digest_for_ticker(cand.ticker, db_path=target_duck)
        days_to_earnings = get_days_until_next_earnings(cand.ticker, as_of_date=r["trade_date"], db_path=target_duck)
        atr_val = float(r["atr14"] or (cand.signal_close * 0.02))
        stop_mult = 3.0 if cand.strategy_name == "rsi_mean_reversion" else 2.5

        band = int(r["liquidity_band"])
        stop_price = round_to_valid_tick(cand.signal_close - stop_mult * atr_val, side="SELL", band=band)
        limit_price = round_to_valid_tick(cand.signal_close, side="BUY", band=band)

        card = evaluate_signal_with_risk_officer(
            ticker=cand.ticker,
            strategy_name=cand.strategy_name,
            horizon_days=20,
            signal_close_price=cand.signal_close,
            atr_stop_loss=stop_price,
            news_context_72h=news_72h,
            days_until_earnings=days_to_earnings,
            duckdb_path=target_duck,
            signal_date=r["trade_date"],
        )

        if not card.approve_recommendation:
            logger.warning(
                "[Agent 3] NO-GO for %s (%s): %s",
                cand.ticker,
                card.rejection_category,
                card.devils_advocate,
            )
            continue

        shares = max(1, int(slot_budget_nok // limit_price))
        queued = enqueue_order_proposal(
            ticker=cand.ticker,
            action="BUY",
            shares=shares,
            signal_close_price=cand.signal_close,
            limit_price=limit_price,
            stop_loss_price=stop_price,
            strategy_name=cand.strategy_name,
            horizon_days=20,
            priority_rank=cand.priority_rank,
            llm_provider=card.llm_provider,
            llm_recommendation="GO" if card.approve_recommendation else "NO-GO",
            llm_confidence=card.confidence,
            risk_score=card.risk_score,
            earnings_blackout_flag=card.earnings_blackout_flag,
            bull_case=card.bull_case,
            devils_advocate=card.devils_advocate,
            db_path=target_sqlite,
        )
        queued_orders.append(queued)
        logger.info(
            f"[Agent 3 & 4] {cand.ticker} ({cand.strategy_name}) -> Status={queued['status']} "
            f"| Limit={limit_price:.2f} NOK | Stop={stop_price:.2f} NOK"
        )

    logger.info("Daglig skanning fullført.")
    return {
        "latest_date": str(latest_date),
        "liquid_uptrend_count": len(trend_liquid_rows),
        "available_slots": available_slots,
        "queued_orders": queued_orders,
    }


if __name__ == "__main__":
    run_daily_scan()
