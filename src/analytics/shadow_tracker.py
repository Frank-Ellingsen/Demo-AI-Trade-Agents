"""Agent 5: Shadow Tracking & Rejection Category Auditor (Cohorts A, B, and C).

Compares:
- Cohort A (`COHORT_A_BASE`): All quantitative signals executed unfiltered.
- Cohort B (`COHORT_B_LLM_GO`): Signals approved by Agent 3 (AI Risk Officer).
- Cohort C (`COHORT_C_LLM_NOGO`): Signals rejected by Agent 3, broken down by
  `rejection_category` ('EARNINGS_SOON', 'COMPANY_SHOCK', 'MACRO_NOISE') to detect
  whether the LLM prompt is saving drawdowns or over-filtering profitable trades due to macro noise.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import duckdb

from config.settings import settings


@dataclass(frozen=True)
class RejectionCategoryAudit:
    """Breakdown of rejected trades (Cohort C) by Agent 3 rejection reason."""

    rejection_category: str
    rejected_count: int
    hypothetical_win_rate_pct: float
    hypothetical_net_pnl_nok: float
    verdict: str


def audit_shadow_cohorts(db_path: Path | None = None) -> tuple[list[dict], list[RejectionCategoryAudit]]:
    """Query DuckDB `trade_ledger` and `signal_evaluations` to compare Cohorts A, B, and C."""
    target_db = db_path or settings.duckdb_path
    if not target_db.exists():
        return [], []

    con = duckdb.connect(str(target_db), read_only=True)
    try:
        cohort_rows = con.execute(
            """
            SELECT
                cohort,
                COUNT(*) AS trades,
                ROUND(AVG(CASE WHEN net_pnl_nok > 0 THEN 100.0 ELSE 0.0 END), 1) AS win_rate_pct,
                ROUND(SUM(gross_pnl_nok), 2) AS gross_pnl_nok,
                ROUND(SUM(commission_nok + slippage_spread_nok + custody_fee_nok), 2) AS friction_nok,
                ROUND(SUM(net_pnl_nok), 2) AS net_pnl_nok,
                ROUND(AVG(pure_strategy_alpha_pct) * 100.0, 2) AS avg_pure_alpha_pct
            FROM trade_ledger
            WHERE broker_profile = 'saxo_classic'
            GROUP BY cohort
            ORDER BY cohort;
            """
        ).fetchall()

        cols = ["cohort", "trades", "win_rate_pct", "gross_pnl_nok", "friction_nok", "net_pnl_nok", "avg_pure_alpha_pct"]
        cohort_summary = [dict(zip(cols, r)) for r in cohort_rows]

        rej_rows = con.execute(
            """
            SELECT
                COALESCE(se.rejection_category, 'UNCLASSIFIED') AS rej_cat,
                COUNT(*) AS cnt,
                ROUND(AVG(CASE WHEN tl.net_pnl_nok > 0 THEN 100.0 ELSE 0.0 END), 1) AS hyp_win_pct,
                ROUND(COALESCE(SUM(tl.net_pnl_nok), 0.0), 2) AS hyp_net_pnl
            FROM signal_evaluations se
            LEFT JOIN trade_ledger tl ON tl.eval_id = se.eval_id AND tl.cohort = 'COHORT_C_LLM_NOGO'
            WHERE se.approve_recommendation = FALSE
            GROUP BY COALESCE(se.rejection_category, 'UNCLASSIFIED')
            ORDER BY cnt DESC;
            """
        ).fetchall()
    finally:
        con.close()

    rej_audits: list[RejectionCategoryAudit] = []
    for cat, cnt, win_pct, net_pnl in rej_rows:
        if cat == "MACRO_NOISE" and (net_pnl or 0.0) > 0:
            verdict = "ADVARSEL: Overfiltrerer vinnere pga. makrostøy -> Stram inn prompt!"
        elif (net_pnl or 0.0) <= 0:
            verdict = "VERDISKAPENDE: Unngikk tap i skyggeporteføljen."
        else:
            verdict = "NØYTRAL / OVERVÅKES"

        rej_audits.append(
            RejectionCategoryAudit(
                rejection_category=str(cat),
                rejected_count=int(cnt),
                hypothetical_win_rate_pct=float(win_pct or 0.0),
                hypothetical_net_pnl_nok=float(net_pnl or 0.0),
                verdict=verdict,
            )
        )

    return cohort_summary, rej_audits
