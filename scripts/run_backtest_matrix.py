"""Run the full OBX 15 Backtest Experiment Matrix (2019-2026 Full + Out-of-Sample YTD) and store in DuckDB."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import settings
from src.backtest.engine import run_portfolio_simulation
from src.backtest.metrics import StrategyMetrics
from src.portfolio.cost_tax_model import NORDNET_MINI, SAXO_CLASSIC
from src.signals.factory import fetch_enriched_signal_panel


def format_tufte_table(results: list[StrategyMetrics], title: str) -> str:
    """Format backtest results adhering to Edward Tufte's Data-Ink Ratio rules (aligned decimals, zero clutter)."""
    lines = [
        f"\n{title}",
        "=" * 118,
        f"{'Strategi':<20} {'Vindu':>5} {'Regime':<6} {'Megler':<13} {'Handler':>7} {'Vinn%':>6} "
        f"{'TotAvk%':>8} {'CAGR%':>6} {'Sharpe':>6} {'Sortino':>7} {'MaxDD%':>7} {'CostDrag%':>9} {'Alfa/år%':>8} {'BetaOBX':>7}",
        "-" * 118,
    ]
    for m in results:
        reg_str = "EKSOG" if m.exogenous_enabled else "ENDOG"
        horizon_str = f"1-{m.horizon_days}d"
        lines.append(
            f"{m.strategy_name:<20} {horizon_str:>5} {reg_str:<6} {m.broker_name:<13} "
            f"{m.total_trades:>7d} {m.win_rate_pct:>6.1f} {m.total_return_pct:>8.2f} "
            f"{m.cagr_pct:>6.2f} {m.sharpe_ratio:>6.2f} {m.sortino_ratio:>7.2f} "
            f"{m.max_drawdown_pct:>7.2f} {m.cost_drag_pct:>9.2f} "
            f"{m.alpha_annualized_pct:>8.2f} {m.beta_obx:>7.2f}"
        )
    lines.append("=" * 118)
    return "\n".join(lines)


def main() -> None:
    print("[1/3] Querying DuckDB Signal Factory for OBX 15 indicator & macro panel (2019-2026)...")
    panel = fetch_enriched_signal_panel()
    print(f"      Loaded {len(panel):,} enriched stock-day observations (post-SMA200 warmup).")

    strategies = ["rsi_mean_reversion", "trend_breakout", "combined_rsi_breakout", "bollinger_squeeze"]
    horizons = [10, 20, 30]
    brokers = [SAXO_CLASSIC, NORDNET_MINI]

    full_results: list[StrategyMetrics] = []
    ytd_results: list[StrategyMetrics] = []
    all_ledger_trades: list[dict] = []

    print("[2/3] Running Full Matrix (3 Strategies x 3 Windows x Exogenous/Endogenous x Saxo/Nordnet)...")
    for strat in strategies:
        for h in horizons:
            for exog in [True, False]:
                for brk in brokers:
                    m_full, trades_full = run_portfolio_simulation(
                        panel_rows=panel,
                        strategy_name=strat,
                        horizon_days=h,
                        exogenous_enabled=exog,
                        broker=brk,
                        period_label="FULL_2019_2026",
                    )
                    full_results.append(m_full)
                    if exog:
                        all_ledger_trades.extend(trades_full)

            # Also run Out-of-Sample (last 12m / YTD window from 2025-01-01) with Exogenous=True on Saxo
            m_ytd, _ = run_portfolio_simulation(
                panel_rows=panel,
                strategy_name=strat,
                horizon_days=h,
                exogenous_enabled=True,
                broker=SAXO_CLASSIC,
                period_label="OOS_2025_2026",
                start_date=date(2025, 1, 1),
            )
            ytd_results.append(m_ytd)

    print("[3/3] Persisting Cohort A baseline trade ledger to DuckDB `trade_ledger`...")
    con = duckdb.connect(str(settings.duckdb_path))
    try:
        con.execute("DELETE FROM trade_ledger WHERE cohort = 'COHORT_A_BASE';")
        if all_ledger_trades:
            rows = [
                (
                    t["trade_id"],
                    t["eval_id"],
                    t["cohort"],
                    t["broker_profile"],
                    t["strategy_name"],
                    t["horizon_window"],
                    t["ticker"],
                    t["sector"],
                    t["signal_date"],
                    t["entry_date"],
                    t["exit_date"],
                    t["shares"],
                    t["entry_raw_price"],
                    t["exit_raw_price"],
                    t["gross_pnl_nok"],
                    t["commission_nok"],
                    t["slippage_spread_nok"],
                    t["custody_fee_nok"],
                    t["net_pnl_nok"],
                    t["net_return_pct"],
                    t["obx_benchmark_return_pct"],
                    t["sector_rel_return_pct"],
                    t["pure_strategy_alpha_pct"],
                    t["exit_reason"],
                )
                for t in all_ledger_trades
            ]
            con.executemany(
                """
                INSERT OR REPLACE INTO trade_ledger VALUES (
                    ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                );
                """,
                rows,
            )
    finally:
        con.close()

    # Filter main display to Saxo vs Nordnet with Exogenous Regime Filter active
    saxo_vs_nordnet = [r for r in full_results if r.exogenous_enabled]
    print(format_tufte_table(saxo_vs_nordnet, "TABELL 1: OBX 15 EKSPERIMENTMATRISE (2019-2026) — MED EKSOGENT REGIMEFILTER (SAXO VS. NORDNET)"))

    endog_vs_exog = [r for r in full_results if r.broker_name == "saxo_classic" and r.horizon_days == 20]
    print(format_tufte_table(endog_vs_exog, "TABELL 2: EFFEKT AV EKSOGENT FILTER (OBX>SMA200, VIX<25, BRENT) — 1-20 DAGER (SAXO CLASSIC)"))

    print(format_tufte_table(ytd_results, "TABELL 3: OUT-OF-SAMPLE / SISTE PERIODE (2025-2026) — FASTFROSNE PARAMETERE (SAXO CLASSIC)"))


if __name__ == "__main__":
    main()
