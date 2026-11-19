"""Agent 1: Endogenous price and volume indicators calculated on dividend-adjusted close (adj_close)."""

from __future__ import annotations


def compute_sma(values: list[float], period: int) -> float:
    """Compute Simple Moving Average over the last `period` bars."""
    if len(values) < period or period <= 0:
        raise ValueError(f"Insufficient data for SMA({period}).")
    window = values[-period:]
    return sum(window) / period


def compute_rsi(closes: list[float], period: int = 2) -> float:
    """Compute Wilder's / simple short-window RSI over the last `period` changes."""
    if len(closes) < period + 1:
        raise ValueError(f"Need at least {period + 1} closes for RSI({period}).")

    gains = 0.0
    losses = 0.0
    recent = closes[-(period + 1) :]
    for i in range(1, len(recent)):
        diff = recent[i] - recent[i - 1]
        if diff > 0:
            gains += diff
        elif diff < 0:
            losses += abs(diff)

    if losses == 0.0:
        return 100.0 if gains > 0 else 50.0
    rs = (gains / period) / (losses / period)
    return 100.0 - (100.0 / (1.0 + rs))


def check_rsi_mean_reversion_entry(
    adj_closes: list[float],
    sma_trend_period: int = 200,
    rsi_period: int = 2,
    rsi_threshold: float = 10.0,
) -> tuple[bool, float]:
    """Check if adj_close > SMA(200) and RSI(2) < 10. Returns (triggered, priority_rank)."""
    sma200 = compute_sma(adj_closes, sma_trend_period)
    current_close = adj_closes[-1]
    rsi_val = compute_rsi(adj_closes, rsi_period)
    triggered = current_close > sma200 and rsi_val < rsi_threshold
    # Lower RSI(2) gives higher priority rank (100 - RSI)
    return triggered, round(100.0 - rsi_val, 4)
