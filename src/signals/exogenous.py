"""Agent 1: Quantitative Signal Engine (Endogenous technical triggers + Exogenous regime filters)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExogenousRegimeState:
    """Current market and commodity regime snapshot."""

    obx_close: float
    obx_sma200: float
    vix_close: float
    brent_2d_pct: float
    sector_rel_momentum_10d: float


def check_exogenous_regime(
    state: ExogenousRegimeState,
    sector: str,
    max_vix: float = 25.0,
    min_brent_2d_for_energy: float = -0.025,
    brent_vix_override_threshold: float = 0.015,
) -> tuple[bool, str]:
    """Evaluate exogenous market, VIX, commodity, and sector momentum filters."""
    if state.obx_close <= state.obx_sma200:
        return False, "BLOCKED_OBX_BELOW_SMA200"

    is_energy_or_shipping = sector in {"Energy", "Shipping"}
    if state.vix_close >= max_vix:
        # Geopolitical spike override for Energy/Shipping when Brent is surging
        if not (is_energy_or_shipping and state.brent_2d_pct >= brent_vix_override_threshold):
            return False, f"BLOCKED_HIGH_VIX ({state.vix_close:.1f} >= {max_vix:.1f})"

    if sector == "Energy" and state.brent_2d_pct <= min_brent_2d_for_energy:
        return False, f"BLOCKED_BRENT_DROP ({state.brent_2d_pct * 100:.2f}%)"

    if state.sector_rel_momentum_10d <= 0.0:
        return False, "BLOCKED_NEGATIVE_SECTOR_MOMENTUM"

    return True, "REGIME_OK"
