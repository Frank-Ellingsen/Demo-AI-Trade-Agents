"""Performance, risk, and Cost Drag metrics for the OBX 15 trading desk."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class StrategyMetrics:
    """Comprehensive performance and Stage-Gate evaluation metrics."""

    strategy_name: str
    horizon_days: int
    exogenous_enabled: bool
    broker_name: str
    period_label: str
    total_trades: int
    cancelled_gap_count: int
    win_rate_pct: float
    total_return_pct: float
    cagr_pct: float
    sharpe_ratio: float
    sortino_ratio: float
    max_drawdown_pct: float
    gross_profit_nok: float
    total_friction_nok: float
    cost_drag_pct: float
    avg_pure_alpha_pct: float
    alpha_annualized_pct: float
    beta_obx: float
    beta_brent: float
    beta_usdnok: float
    passes_all_gates: bool


def compute_portfolio_metrics(
    daily_returns: list[float],
    equity_curve: list[float],
    trading_days_per_year: int = 252,
) -> tuple[float, float, float, float, float]:
    """Return (total_return_pct, cagr_pct, sharpe, sortino, max_drawdown_pct)."""
    if not equity_curve or len(equity_curve) < 2:
        return 0.0, 0.0, 0.0, 0.0, 0.0

    start_eq = equity_curve[0]
    end_eq = equity_curve[-1]
    total_ret = (end_eq - start_eq) / start_eq if start_eq > 0 else 0.0

    n_days = max(1, len(daily_returns))
    years = n_days / trading_days_per_year
    if end_eq > 0 and start_eq > 0 and years > 0:
        cagr = (end_eq / start_eq) ** (1.0 / years) - 1.0
    else:
        cagr = 0.0

    if n_days < 2:
        return total_ret * 100.0, cagr * 100.0, 0.0, 0.0, 0.0

    mean_ret = sum(daily_returns) / n_days
    var_all = sum((r - mean_ret) ** 2 for r in daily_returns) / (n_days - 1)
    std_all = math.sqrt(var_all) if var_all > 0 else 0.0
    sharpe = (mean_ret / std_all) * math.sqrt(trading_days_per_year) if std_all > 1e-12 else 0.0

    # Downside deviation (MAR = 0) across active return days
    downside_sq = [min(0.0, r) ** 2 for r in daily_returns]
    downside_dev = math.sqrt(sum(downside_sq) / n_days)
    sortino = (mean_ret / downside_dev) * math.sqrt(trading_days_per_year) if downside_dev > 1e-12 else 0.0

    # Max drawdown
    peak = equity_curve[0]
    max_dd = 0.0
    for eq in equity_curve:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd

    return (
        round(total_ret * 100.0, 2),
        round(cagr * 100.0, 2),
        round(sharpe, 2),
        round(sortino, 2),
        round(max_dd * 100.0, 2),
    )
