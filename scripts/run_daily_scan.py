"""Daily EOD Orchestrator (16:40 CET): Agent 1 -> Agent 2 -> Agent 3 -> Agent 4.

1. Agent 1 scans the latest closing bar across the 15 OBX stocks in DuckDB.
2. Agent 2 packages the 48-72h NewsWeb RSS digest and checks the 3-day Earnings Guardrail.
3. Agent 3 (Hybrid Ollama / Gemini) evaluates tail risk and logs to DuckDB `signal_evaluations`.
4. Agent 4 sizes the position (25% slot, max 4 total, max 2/sector), rounds the limit price to
   Oslo Børs MiFID II tick-size, and enqueues the proposal in SQLite WAL (`hitl_orders.db`) with
   a 14-hour auto-expiry timer.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import settings
from src.backtest.engine import evaluate_entry_trigger
from src.data_ingestion.corporate_actions import get_days_until_next_earnings
from src.data_ingestion.newsweb_rss import build_72h_news_digest_for_ticker
from src.db.duckdb_store import init_duckdb
from src.db.sqlite_store import init_sqlite_db
from src.hitl.llm_validator import evaluate_signal_with_risk_officer
from src.hitl.order_queue import enqueue_order_proposal, expire_stale_orders
from src.portfolio.allocator import CandidateSignal, select_portfolio_candidates
from src.portfolio.cost_tax_model import round_to_tick_size
from src.signals.exogenous import ExogenousRegimeState, check_exogenous_regime
from src.signals.factory import fetch_enriched_signal_panel


def main() -> None:
    init_sqlite_db()
    init_duckdb()

    expired = expire_stale_orders()
    if expired:
        print(f"[Agent 4] Expired {expired} stale orders (>14h old).")

    panel = fetch_enriched_signal_panel()
    if not panel:
        print("[Agent 1] No enriched signal rows found in DuckDB. Run scripts/ingest_market_data.py first.")
        return

    latest_date = max(r["trade_date"] for r in panel)
    latest_rows = [r for r in panel if r["trade_date"] == latest_date]
    print(f"[Agent 1] Scanning OBX 15 EOD bar for trade_date = {latest_date} ({len(latest_rows)} stocks)...")

    raw_candidates: list[CandidateSignal] = []
    row_by_ticker: dict[str, dict] = {}

    for r in latest_rows:
        row_by_ticker[r["ticker"]] = r
        reg = ExogenousRegimeState(
            obx_close=float(r["obx_close"] or 0.0),
            obx_sma200=float(r["obx_sma200"] or 0.0),
            vix_close=float(r["vix_close"] or 18.0),
            brent_2d_pct=float(r["brent_2d_pct"] or 0.0),
            sector_rel_momentum_10d=float(r["sector_rel_mom_10d"] or 0.0),
        )
        ok_reg, reg_reason = check_exogenous_regime(reg, sector=r["sector"])
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
        active_positions_by_sector={},
        current_open_positions=0,
        max_total_positions=settings.max_concurrent_positions,
        max_per_sector=settings.max_positions_per_sector,
    )

    if not selected:
        print(f"[Agent 1] 0 actionable BUY candidates triggered on {latest_date}. Cash rule active (0% drag).")
        return

    slot_budget_nok = settings.initial_capital_nok / settings.max_concurrent_positions

    for cand in selected:
        r = row_by_ticker[cand.ticker]
        news_72h = build_72h_news_digest_for_ticker(cand.ticker)
        days_to_earnings = get_days_until_next_earnings(cand.ticker, as_of_date=latest_date)
        atr_val = float(r["atr14"] or (cand.signal_close * 0.02))
        stop_mult = 3.0 if cand.strategy_name == "rsi_mean_reversion" else 2.5
        stop_price = round_to_tick_size(
            cand.signal_close - stop_mult * atr_val,
            liquidity_band=int(r["liquidity_band"]),
            side="SELL",
        )
        limit_price = round_to_tick_size(
            cand.signal_close,
            liquidity_band=int(r["liquidity_band"]),
            side="BUY",
        )

        card = evaluate_signal_with_risk_officer(
            ticker=cand.ticker,
            strategy_name=cand.strategy_name,
            horizon_days=20,
            signal_close_price=cand.signal_close,
            atr_stop_loss=stop_price,
            news_context_72h=news_72h,
            days_until_earnings=days_to_earnings,
        )

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
        )
        print(
            f"[Agent 3 & 4] {cand.ticker} ({cand.strategy_name}) -> AI={queued['status']} "
            f"| OrderID={queued['order_id']} | Limit={limit_price:.2f} NOK | Stop={stop_price:.2f} NOK"
        )


if __name__ == "__main__":
    main()
