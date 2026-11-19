"""Agent 5 CLI (Sunday Evening Auditor): Performance Attribution, Cost Drag & Shadow Tracking Report."""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import settings
from src.analytics.shadow_tracker import audit_shadow_cohorts


def main() -> None:
    con = duckdb.connect(str(settings.duckdb_path), read_only=True)
    try:
        strat_rows = con.execute(
            """
            SELECT
                strategy_name,
                horizon_window,
                COUNT(*) AS trades,
                ROUND(AVG(CASE WHEN net_pnl_nok > 0 THEN 100.0 ELSE 0.0 END), 1) AS win_pct,
                ROUND(SUM(gross_pnl_nok), 2) AS gross_nok,
                ROUND(SUM(commission_nok + slippage_spread_nok + custody_fee_nok), 2) AS cost_nok,
                ROUND(SUM(net_pnl_nok), 2) AS net_nok,
                ROUND(AVG(obx_benchmark_return_pct) * 100.0, 2) AS mkt_pct,
                ROUND(AVG(sector_rel_return_pct) * 100.0, 2) AS sec_pct,
                ROUND(AVG(pure_strategy_alpha_pct) * 100.0, 2) AS alpha_pct
            FROM trade_ledger
            WHERE broker_profile = 'saxo_classic'
              AND strategy_name != 'bollinger_squeeze'
            GROUP BY strategy_name, horizon_window
            ORDER BY strategy_name, horizon_window;
            """
        ).fetchall()
    finally:
        con.close()

    print("\nAGENT 5 UKENTLIG REVISJON: FAKTORDEKOMPONERING & COST DRAG (SAXO CLASSIC)")
    print("=" * 108)
    print(
        f"{'Strategi':<24} {'Vindu':>5} {'Handler':>7} {'Vinn%':>6} "
        f"{'Brutto NOK':>11} {'Friksjon NOK':>13} {'Netto NOK':>10} {'Marked%':>8} {'Sektor%':>8} {'RenAlfa%':>9}"
    )
    print("-" * 108)
    for r in strat_rows:
        strat, hw, tr, win, gross, cost, net, mkt, sec, alpha = r
        print(
            f"{strat:<24} {f'1-{hw}d':>5} {tr:>7d} {win:>6.1f} "
            f"{gross:>11.2f} {cost:>13.2f} {net:>10.2f} {mkt:>8.2f} {sec:>8.2f} {alpha:>9.2f}"
        )
    print("=" * 108)

    cohort_summary, rej_audits = audit_shadow_cohorts()
    print("\nSHADOW TRACKING (KOHORT A vs. KOHORT B vs. KOHORT C)")
    print("-" * 88)
    for c in cohort_summary:
        print(
            f"Kohort: {c['cohort']:<18} | Handler: {c['trades']:>5d} | Vinn%: {c['win_rate_pct']:>5.1f}% "
            f"| Netto NOK: {c['net_pnl_nok']:>10.2f} | Snitt Ren Alfa/handel: {c['avg_pure_alpha_pct']:>6.2f}%"
        )

    if rej_audits:
        print("\nAVSLAGSTAKSONOMI I KOHORT C (EARNINGS_SOON vs. COMPANY_SHOCK vs. MACRO_NOISE)")
        print("-" * 88)
        for a in rej_audits:
            print(
                f"Kategori: {a.rejection_category:<15} | Avslått: {a.rejected_count:>4d} "
                f"| Hyp. Netto NOK: {a.hypothetical_net_pnl_nok:>9.2f} | Status: {a.verdict}"
            )


if __name__ == "__main__":
    main()
