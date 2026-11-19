"""Export live DuckDB & SQLite WAL state into `ui/dashboard_data.json` and update `ui/index.html`."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import settings
from src.analytics.shadow_tracker import audit_shadow_cohorts
from src.backtest.engine import run_portfolio_simulation
from src.db.sqlite_store import get_sqlite_conn
from src.portfolio.cost_tax_model import NORDNET_MINI, SAXO_CLASSIC, get_mifid2_tick_size
from src.signals.factory import fetch_enriched_signal_panel


def build_dashboard_payload() -> dict[str, Any]:
    """Query DuckDB and SQLite WAL to build the complete JSON payload for the UI."""
    panel = fetch_enriched_signal_panel()

    strategies = ["combined_rsi_breakout", "rsi_mean_reversion", "trend_breakout"]
    horizons = [10, 20, 30]
    matrix_rows: list[dict[str, Any]] = []

    for strat in strategies:
        for h in horizons:
            for brk in (SAXO_CLASSIC, NORDNET_MINI):
                m, _ = run_portfolio_simulation(
                    panel_rows=panel,
                    strategy_name=strat,
                    horizon_days=h,
                    exogenous_enabled=True,
                    broker=brk,
                    period_label="2019-2026",
                )
                matrix_rows.append(
                    {
                        "strategy": m.strategy_name,
                        "window": f"1-{m.horizon_days}d",
                        "horizon_days": m.horizon_days,
                        "broker": m.broker_name,
                        "trades": m.total_trades,
                        "win_rate_pct": m.win_rate_pct,
                        "total_return_pct": m.total_return_pct,
                        "cagr_pct": m.cagr_pct,
                        "sharpe": m.sharpe_ratio,
                        "sortino": m.sortino_ratio,
                        "max_dd_pct": m.max_drawdown_pct,
                        "cost_drag_pct": m.cost_drag_pct,
                        "alpha_ann_pct": m.alpha_annualized_pct,
                        "beta_obx": m.beta_obx,
                        "beta_brent": m.beta_brent,
                        "gross_profit_nok": m.gross_profit_nok,
                        "total_friction_nok": m.total_friction_nok,
                    }
                )

    # Latest OBX 15 stock snapshot & macro regime
    latest_date = max(r["trade_date"] for r in panel)
    latest_bars = [r for r in panel if r["trade_date"] == latest_date]
    sample_macro = latest_bars[0] if latest_bars else {}

    # Build 12-day price series and 20-day ADV MNOK per stock from DuckDB
    con_snap = duckdb.connect(str(settings.duckdb_path), read_only=True)
    try:
        series_rows = con_snap.execute(
            """
            WITH ranked AS (
                SELECT
                    ticker,
                    raw_close,
                    turnover_nok,
                    ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY trade_date DESC) AS rn
                FROM ohlcv_daily
                WHERE ticker LIKE '%.OL' AND ticker != 'OBX.OL'
            )
            SELECT
                ticker,
                LIST(ROUND(raw_close, 2) ORDER BY rn DESC) FILTER (WHERE rn <= 12) AS series_12d,
                ROUND(AVG(turnover_nok) FILTER (WHERE rn <= 20) / 1000000.0, 1) AS adv_mnok
            FROM ranked
            WHERE rn <= 20
            GROUP BY ticker;
            """
        ).fetchall()
        extra_by_ticker = {r[0]: (list(r[1] or []), float(r[2] or 100.0)) for r in series_rows}
    finally:
        con_snap.close()

    obx15_snapshot = [
        {
            "ticker": r["ticker"],
            "sector": r["sector"],
            "liquidity_band": int(r["liquidity_band"]),
            "raw_close": round(float(r["raw_close"]), 2),
            "tick_size": get_mifid2_tick_size(float(r["raw_close"]), int(r["liquidity_band"])),
            "adv_mnok": extra_by_ticker.get(r["ticker"], ([], 100.0))[1],
            "series_12d": extra_by_ticker.get(r["ticker"], ([], 100.0))[0],
            "rsi2": round(float(r["rsi2"] or 50.0), 1),
            "ret_3d_pct": round(float(r["ret_3d"] or 0.0) * 100.0, 2),
            "ret_10d_pct": round(float(r["ret_10d"] or 0.0) * 100.0, 2),
            "vol_ratio": round(float(r["vol_ratio"] or 1.0), 2),
            "above_sma200": bool(r["adj_close"] and r["sma200"] and r["adj_close"] > r["sma200"]),
        }
        for r in sorted(latest_bars, key=lambda x: x["ticker"])
    ]

    # Recent trades & cumulative equity curve from DuckDB
    con = duckdb.connect(str(settings.duckdb_path), read_only=True)
    try:
        recent_trades_raw = con.execute(
            """
            SELECT
                trade_id, strategy_name, horizon_window, ticker, sector,
                CAST(entry_date AS VARCHAR) AS entry_date,
                CAST(exit_date AS VARCHAR) AS exit_date,
                shares, entry_raw_price, exit_raw_price,
                gross_pnl_nok,
                ROUND(commission_nok + slippage_spread_nok + custody_fee_nok, 2) AS friction_nok,
                net_pnl_nok,
                ROUND(net_return_pct * 100.0, 2) AS net_return_pct,
                ROUND(obx_benchmark_return_pct * 100.0, 2) AS mkt_pct,
                ROUND(sector_rel_return_pct * 100.0, 2) AS sec_pct,
                ROUND(pure_strategy_alpha_pct * 100.0, 2) AS alpha_pct,
                exit_reason
            FROM trade_ledger
            WHERE broker_profile = 'saxo_classic'
              AND strategy_name = 'combined_rsi_breakout'
              AND horizon_window = 20
            ORDER BY entry_date DESC
            LIMIT 40;
            """
        ).fetchall()
        t_cols = [
            "trade_id", "strategy_name", "horizon_window", "ticker", "sector",
            "entry_date", "exit_date", "shares", "entry_raw_price", "exit_raw_price",
            "gross_pnl_nok", "friction_nok", "net_pnl_nok", "net_return_pct",
            "mkt_pct", "sec_pct", "alpha_pct", "exit_reason",
        ]
        recent_trades = [dict(zip(t_cols, row)) for row in recent_trades_raw]

        eval_rows_raw = con.execute(
            """
            SELECT
                eval_id, CAST(signal_date AS VARCHAR) AS signal_date,
                ticker, strategy_name, llm_provider,
                approve_recommendation, rejection_category,
                confidence, risk_score, earnings_blackout_flag,
                bull_case, devils_advocate
            FROM signal_evaluations
            ORDER BY evaluated_at DESC
            LIMIT 25;
            """
        ).fetchall()
        e_cols = [
            "eval_id", "signal_date", "ticker", "strategy_name", "llm_provider",
            "approve_recommendation", "rejection_category", "confidence",
            "risk_score", "earnings_blackout_flag", "bull_case", "devils_advocate",
        ]
        evaluations = [dict(zip(e_cols, row)) for row in eval_rows_raw]
    finally:
        con.close()

    # Pending HITL orders from SQLite WAL
    pending_orders: list[dict[str, Any]] = []
    if settings.sqlite_db_path.exists():
        with get_sqlite_conn() as sconn:
            rows = sconn.execute(
                """
                SELECT
                    order_id, x_request_id, ticker, action, order_type, shares,
                    signal_close_price, limit_price, stop_loss_price,
                    strategy_name, horizon_days, llm_provider, llm_recommendation,
                    risk_score, earnings_blackout_flag, bull_case, devils_advocate,
                    status, created_at, expires_at
                FROM pending_orders
                ORDER BY created_at DESC
                LIMIT 25;
                """
            ).fetchall()
            pending_orders = [dict(r) for r in rows]

    cohort_summary, rej_audits = audit_shadow_cohorts()

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "latest_trade_date": str(latest_date),
        "macro_regime": {
            "obx_close": round(float(sample_macro.get("obx_close") or 0.0), 2),
            "obx_sma200": round(float(sample_macro.get("obx_sma200") or 0.0), 2),
            "vix_close": round(float(sample_macro.get("vix_close") or 18.0), 2),
            "brent_2d_pct": round(float(sample_macro.get("brent_2d_pct") or 0.0) * 100.0, 2),
        },
        "matrix_results": matrix_rows,
        "obx15_snapshot": obx15_snapshot,
        "recent_trades": recent_trades,
        "signal_evaluations": evaluations,
        "pending_orders": pending_orders,
        "cohort_summary": cohort_summary,
        "rejection_audits": [
            {
                "rejection_category": a.rejection_category,
                "rejected_count": a.rejected_count,
                "hypothetical_win_rate_pct": a.hypothetical_win_rate_pct,
                "hypothetical_net_pnl_nok": a.hypothetical_net_pnl_nok,
                "verdict": a.verdict,
            }
            for a in rej_audits
        ],
    }


def main() -> None:
    ui_dir = settings.base_dir / "ui"
    ui_dir.mkdir(parents=True, exist_ok=True)
    payload = build_dashboard_payload()
    out_json = ui_dir / "dashboard_data.json"
    out_json.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[OK] Exported UI data snapshot to {out_json}")


if __name__ == "__main__":
    main()
