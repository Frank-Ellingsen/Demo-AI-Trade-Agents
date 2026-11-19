"""
Multi-Universe US Systematic Backtester
========================================
Modellerer og sammenligner 4 investeringsstrategier mot S&P 500:
1. Liquid S&P Momentum (0 kurtasje hos Alpaca, asymmetrisk R/R)
2. OTC / Micro-Caps (1.6% bid-ask spread friksjon og illikviditet)
3. Systematic Covered Calls (SPY + selge manedlige 2.5% OTM calls)
4. AI Regime Switcher (VIX-styrt rotasjon mellom momentum, calls og T-Bills)
"""

from __future__ import annotations

import numpy as np
import pandas as pd


STRATEGY_COLUMNS = [
    "S&P 500 Benchmark",
    "1. Liquid S&P Momentum",
    "2. OTC Micro-Caps (Netto)",
    "3. Systematic Covered Calls",
    "4. AI Regime Switcher (VIX)",
]


def run_us_backtest(
    initial_capital: float = 100_000.0,
    days: int = 252,
    seed: int = 42,
) -> pd.DataFrame:
    """Simulate 252-day equity curves across S&P 500 and 4 systematic US strategies."""
    rng = np.random.default_rng(seed)
    rf = 0.045
    init_cap = float(initial_capital)

    # 1. S&P 500 Benchmark
    spy_daily = rng.normal(0.00045, 0.010, days)
    spy_curve = init_cap * np.cumprod(1.0 + spy_daily)

    # 2. Liquid S&P Momentum (Vinneren - kapper tap raskt med ATR stop-loss)
    mom_daily = spy_daily * 1.35 + rng.normal(0.0003, 0.008, days)
    mom_daily = np.maximum(mom_daily, -0.018)
    mom_curve = init_cap * np.cumprod(1.0 + mom_daily)

    # 3. OTC Micro-Caps (Spises opp av 1.6% spread pr mnd)
    otc_daily = rng.normal(0.0003, 0.022, days) - (0.016 / 21.0)
    otc_curve = init_cap * np.cumprod(1.0 + otc_daily)

    # 4. Systematic Covered Calls (SPY + premie - capped oppside)
    cc_daily = np.minimum(spy_daily, 0.0018) + (0.0135 / 21.0)
    cc_curve = init_cap * np.cumprod(1.0 + cc_daily)

    # 5. AI Regime Switcher (VIX-styrt rotasjon)
    vix = rng.normal(17.0, 4.0, days)
    switcher_daily = np.where(vix < 18.0, mom_daily, np.where(vix < 24.0, cc_daily, rf / 252.0))
    switcher_curve = init_cap * np.cumprod(1.0 + switcher_daily)

    return pd.DataFrame(
        {
            "Dag": np.arange(1, days + 1),
            "S&P 500 Benchmark": np.round(spy_curve, 2),
            "1. Liquid S&P Momentum": np.round(mom_curve, 2),
            "2. OTC Micro-Caps (Netto)": np.round(otc_curve, 2),
            "3. Systematic Covered Calls": np.round(cc_curve, 2),
            "4. AI Regime Switcher (VIX)": np.round(switcher_curve, 2),
        }
    )


def calculate_metrics(df: pd.DataFrame, initial_capital: float = 100_000.0) -> pd.DataFrame:
    """Calculate annualized return, volatility, max drawdown, Sharpe, and Sortino for each strategy."""
    rf = 0.045
    init_cap = float(initial_capital)
    days = max(len(df), 1)
    rows = []

    for col in STRATEGY_COLUMNS:
        if col not in df.columns:
            continue
        curve = df[col].to_numpy(dtype=float)
        prev = np.concatenate(([init_cap], curve[:-1]))
        daily = (curve - prev) / np.where(prev == 0, 1.0, prev)

        end_val = float(curve[-1])
        ret_pct = (end_val - init_cap) / init_cap * 100.0
        vol_pct = float(np.std(daily) * np.sqrt(252) * 100.0)
        running_max = np.maximum.accumulate(np.concatenate(([init_cap], curve)))[1:]
        max_dd_pct = float(np.max(1.0 - curve / running_max) * 100.0)
        ann_ret = (end_val / init_cap) ** (252.0 / days) - 1.0
        sharpe = (ann_ret - rf) / (vol_pct / 100.0) if vol_pct > 0 else 0.0
        downside = daily[daily < 0]
        down_std = float(np.std(downside) * np.sqrt(252)) if len(downside) > 0 else 0.0
        sortino = (ann_ret - rf) / down_std if down_std > 0 else 0.0

        rows.append(
            {
                "Strategi": col,
                "Sluttverdi ($)": round(end_val, 2),
                "Total Avkastning (%)": round(ret_pct, 2),
                "Volatilitet (%)": round(vol_pct, 2),
                "Max Drawdown (%)": round(max_dd_pct, 2),
                "Sharpe Ratio": round(sharpe, 2),
                "Sortino Ratio": round(sortino, 2),
            }
        )

    return pd.DataFrame(rows)


def run_us_backtests() -> pd.DataFrame:
    """CLI entrypoint printing formatted backtest comparison table."""
    df = run_us_backtest(initial_capital=100_000.0, days=252, seed=42)
    metrics = calculate_metrics(df, initial_capital=100_000.0)
    print("=" * 94)
    print("US MULTI-UNIVERSE SYSTEMATIC BACKTEST (252 HANDELSDAGER, $100,000 USD)")
    print("=" * 94)
    print(metrics.to_string(index=False))
    print("=" * 94)
    return metrics


if __name__ == "__main__":
    run_us_backtests()
