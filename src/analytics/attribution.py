"""Agent 5: Factor decomposition and Multi-Factor OLS Regression (Market, Sector, Brent, FX, and Pure Alpha).

Implements:
1. Per-trade Return Decomposition:
   Net Return = Markedsbidrag (OBX) + Sektorbidrag + Ren Strategi-Alfa
2. Multi-Factor Time-Series Regression (Fama-French / Oslo Børs Macro style):
   R_{p,t} - R_{f,t} = alpha + beta_OBX * (R_{OBX,t} - R_{f,t}) + beta_Brent * dBrent_t + beta_FX * dUSDNOK_t + epsilon_t
   Solved deterministically in pure Python (Gaussian elimination on normal equations X^T X b = X^T y).
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class ReturnAttribution:
    """Decomposed return components for a single closed position."""

    net_return_pct: float
    market_contribution_pct: float
    sector_contribution_pct: float
    pure_strategy_alpha_pct: float


@dataclass(frozen=True)
class MultiFactorRegressionResult:
    """Result of the Oslo Børs 3-factor macro regression on daily portfolio excess returns."""

    alpha_daily: float
    alpha_annualized: float
    beta_obx: float
    beta_brent: float
    beta_usdnok: float
    r_squared: float
    idiosyncratic_vol_daily: float
    idiosyncratic_vol_annualized: float
    observations: int


def decompose_trade_return(
    net_return_pct: float,
    obx_return_pct: float,
    sector_index_return_pct: float,
) -> ReturnAttribution:
    """Decompose Net Return = OBX Market Contribution + Relative Sector Contribution + Pure Alpha."""
    market_contrib = obx_return_pct
    sector_contrib = sector_index_return_pct - obx_return_pct
    pure_alpha = net_return_pct - market_contrib - sector_contrib

    return ReturnAttribution(
        net_return_pct=round(net_return_pct, 6),
        market_contribution_pct=round(market_contrib, 6),
        sector_contribution_pct=round(sector_contrib, 6),
        pure_strategy_alpha_pct=round(pure_alpha, 6),
    )


def run_multifactor_ols_regression(
    portfolio_excess_returns: list[float],
    obx_excess_returns: list[float],
    brent_returns: list[float],
    usdnok_returns: list[float],
    trading_days_per_year: int = 252,
) -> MultiFactorRegressionResult:
    """Estimate R_{p,t} - R_{f,t} = alpha + beta_OBX * R_OBX + beta_Brent * dBrent + beta_FX * dUSDNOK + eps.

    Uses pure Python normal equations with ridge regularization fallback for numerical stability.
    """
    n = len(portfolio_excess_returns)
    if n < 5 or not (len(obx_excess_returns) == len(brent_returns) == len(usdnok_returns) == n):
        raise ValueError("All factor series must have identical length >= 5.")

    # Design matrix rows: [1.0, obx_t, brent_t, fx_t]
    X = [
        [1.0, obx_excess_returns[i], brent_returns[i], usdnok_returns[i]]
        for i in range(n)
    ]
    y = list(portfolio_excess_returns)

    k = 4
    xtx = [[0.0] * k for _ in range(k)]
    xty = [0.0] * k

    for i in range(n):
        row = X[i]
        yi = y[i]
        for r in range(k):
            xty[r] += row[r] * yi
            for c in range(k):
                xtx[r][c] += row[r] * row[c]

    # Tiny ridge term on slope diagonals to guard against collinearity in flat samples
    for d in range(1, k):
        xtx[d][d] += 1e-12

    coeffs = _solve_linear_system(xtx, xty)
    alpha_d, beta_obx, beta_brent, beta_fx = coeffs

    y_mean = sum(y) / n
    ss_tot = sum((yi - y_mean) ** 2 for yi in y)
    residuals = [
        y[i] - (alpha_d + beta_obx * X[i][1] + beta_brent * X[i][2] + beta_fx * X[i][3])
        for i in range(n)
    ]
    ss_res = sum(r * r for r in residuals)

    r2 = max(0.0, 1.0 - (ss_res / ss_tot)) if ss_tot > 1e-15 else 0.0
    dof = max(1, n - k)
    idio_var = ss_res / dof
    idio_vol_d = math.sqrt(max(0.0, idio_var))

    return MultiFactorRegressionResult(
        alpha_daily=round(alpha_d, 6),
        alpha_annualized=round(alpha_d * trading_days_per_year, 4),
        beta_obx=round(beta_obx, 4),
        beta_brent=round(beta_brent, 4),
        beta_usdnok=round(beta_fx, 4),
        r_squared=round(r2, 4),
        idiosyncratic_vol_daily=round(idio_vol_d, 6),
        idiosyncratic_vol_annualized=round(idio_vol_d * math.sqrt(trading_days_per_year), 4),
        observations=n,
    )


def _solve_linear_system(a_mat: list[list[float]], b_vec: list[float]) -> list[float]:
    """Solve A * x = b using Gaussian elimination with partial pivoting."""
    n = len(b_vec)
    aug = [row[:] + [b_vec[i]] for i, row in enumerate(a_mat)]

    for col in range(n):
        pivot_row = max(range(col, n), key=lambda r: abs(aug[r][col]))
        aug[col], aug[pivot_row] = aug[pivot_row], aug[col]

        pivot_val = aug[col][col]
        if abs(pivot_val) < 1e-15:
            continue

        for j in range(col, n + 1):
            aug[col][j] /= pivot_val

        for r in range(n):
            if r != col:
                factor = aug[r][col]
                for j in range(col, n + 1):
                    aug[r][j] -= factor * aug[col][j]

    return [aug[i][n] for i in range(n)]
