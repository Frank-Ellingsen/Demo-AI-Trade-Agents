"""Risk manager: 09:00 CET Open Gap Guardrail and ATR stop-loss calculators."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GapCheckResult:
    """Result of the 09:00 CET opening auction gap guardrail check."""

    allowed: bool
    gap_pct: float
    status_reason: str


def check_opening_gap_guardrail(
    signal_close: float,
    next_open: float,
    max_gap_up_pct: float = 0.015,
    max_gap_down_pct: float = -0.020,
) -> GapCheckResult:
    """Verify that T+1 opening price has not gapped outside the acceptable execution band."""
    if signal_close <= 0:
        raise ValueError("signal_close must be positive.")

    gap_pct = (next_open - signal_close) / signal_close
    if gap_pct > max_gap_up_pct:
        return GapCheckResult(
            allowed=False,
            gap_pct=round(gap_pct, 5),
            status_reason=f"CANCELLED_GAP_UP ({gap_pct * 100:.2f}% > +{max_gap_up_pct * 100:.2f}%)",
        )
    if gap_pct < max_gap_down_pct:
        return GapCheckResult(
            allowed=False,
            gap_pct=round(gap_pct, 5),
            status_reason=f"CANCELLED_GAP_DOWN ({gap_pct * 100:.2f}% < {max_gap_down_pct * 100:.2f}%)",
        )
    return GapCheckResult(
        allowed=True,
        gap_pct=round(gap_pct, 5),
        status_reason="OK",
    )
