"""Broker commission, spread/slippage, custody drag, ASK tax model, and full MiFID II RTS 11 Tick Size Engine."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from enum import Enum


class OrderSide(str, Enum):
    """Order execution direction."""

    BUY = "BUY"
    SELL = "SELL"
    NEAREST = "NEAREST"


class LiquidityBand(int, Enum):
    """Euronext Oslo Børs (XOSL) MiFID II Liquidity Bands based on ADNT."""

    BAND_1 = 1  # 0 <= ADNT < 10
    BAND_2 = 2  # 10 <= ADNT < 80
    BAND_3 = 3  # 80 <= ADNT < 600
    BAND_4 = 4  # 600 <= ADNT < 2000
    BAND_5 = 5  # 2000 <= ADNT < 9000 (standard large-cap OBX)
    BAND_6 = 6  # ADNT >= 9000 (EQNR, DNB, NHY)


# Complete MiFID II RTS 11 Tick Size Table across all 16 price tiers and 6 liquidity bands
MIFID_II_TABLE: dict[float, dict[int, float]] = {
    0.1: {1: 0.0005, 2: 0.0002, 3: 0.0001, 4: 0.0001, 5: 0.0001, 6: 0.0001},
    0.2: {1: 0.001, 2: 0.0005, 3: 0.0002, 4: 0.0001, 5: 0.0001, 6: 0.0001},
    0.5: {1: 0.002, 2: 0.001, 3: 0.0005, 4: 0.0002, 5: 0.0001, 6: 0.0001},
    1.0: {1: 0.005, 2: 0.002, 3: 0.001, 4: 0.0005, 5: 0.0002, 6: 0.0001},
    2.0: {1: 0.01, 2: 0.005, 3: 0.002, 4: 0.001, 5: 0.0005, 6: 0.0002},
    5.0: {1: 0.02, 2: 0.01, 3: 0.005, 4: 0.002, 5: 0.001, 6: 0.0005},
    10.0: {1: 0.05, 2: 0.02, 3: 0.01, 4: 0.005, 5: 0.002, 6: 0.001},
    20.0: {1: 0.1, 2: 0.05, 3: 0.02, 4: 0.01, 5: 0.005, 6: 0.002},
    50.0: {1: 0.2, 2: 0.1, 3: 0.05, 4: 0.02, 5: 0.01, 6: 0.005},
    100.0: {1: 0.5, 2: 0.2, 3: 0.1, 4: 0.05, 5: 0.02, 6: 0.01},
    200.0: {1: 1.0, 2: 0.5, 3: 0.2, 4: 0.1, 5: 0.05, 6: 0.02},
    500.0: {1: 2.0, 2: 1.0, 3: 0.5, 4: 0.2, 5: 0.1, 6: 0.05},
    1000.0: {1: 5.0, 2: 2.0, 3: 1.0, 4: 0.5, 5: 0.2, 6: 0.1},
    2000.0: {1: 10.0, 2: 5.0, 3: 2.0, 4: 1.0, 5: 0.5, 6: 0.2},
    5000.0: {1: 20.0, 2: 10.0, 3: 5.0, 4: 2.0, 5: 1.0, 6: 0.5},
    float("inf"): {1: 50.0, 2: 20.0, 3: 10.0, 4: 5.0, 5: 2.0, 6: 1.0},
}


@dataclass(frozen=True)
class BrokerFeeProfile:
    """Broker fee parameters in NOK."""

    name: str
    commission_pct: float
    min_commission_nok: float
    custody_fee_annual_pct: float = 0.0
    half_spread_pct: float = 0.0005
    extra_slippage_pct: float = 0.0005


SAXO_CLASSIC = BrokerFeeProfile(
    name="saxo_classic",
    commission_pct=0.0008,
    min_commission_nok=10.0,
    custody_fee_annual_pct=0.0,
)

NORDNET_MINI = BrokerFeeProfile(
    name="nordnet_mini",
    commission_pct=0.0015,
    min_commission_nok=29.0,
    custody_fee_annual_pct=0.0,
)


@dataclass(frozen=True)
class TradeFrictionSummary:
    """Detailed decomposition of round-trip transaction costs vs. gross profit."""

    entry_notional_nok: float
    exit_notional_nok: float
    gross_pnl_nok: float
    entry_commission_nok: float
    exit_commission_nok: float
    spread_and_slippage_nok: float
    custody_fee_nok: float
    total_cost_nok: float
    net_pnl_nok: float
    cost_drag_pct_of_gross: float | None
    passes_cost_drag_gate: bool


def get_mifid2_tick_size(price: float, band: int | LiquidityBand = LiquidityBand.BAND_5) -> float:
    """Find the Euronext Oslo Børs MiFID II tick size based on price and liquidity band (1-6)."""
    if price <= 0:
        return 0.01
    band_int = int(band)
    for upper_bound, band_dict in sorted(MIFID_II_TABLE.items(), key=lambda x: x[0]):
        if price < upper_bound:
            return band_dict.get(band_int, 0.01)
    return 0.10


def round_to_valid_tick(
    price: float,
    side: str | OrderSide = OrderSide.BUY,
    band: int | LiquidityBand = LiquidityBand.BAND_5,
) -> float:
    """Round a target limit price to a valid Oslo Børs MiFID II tick with boundary re-check.

    Directional rounding logic:
      - BUY: Floor to lower tick (prevents overpaying and aggressive crossing of spread)
      - SELL: Ceil to higher tick (prevents selling below fair limit threshold)
      - NEAREST: Round half up
    """
    if price <= 0:
        raise ValueError("Price must be strictly positive.")

    side_str = side.value if isinstance(side, OrderSide) else str(side).upper()
    rounding_mode = {
        "BUY": ROUND_FLOOR,
        "SELL": ROUND_CEILING,
        "NEAREST": ROUND_HALF_UP,
    }.get(side_str, ROUND_HALF_UP)

    tick = get_mifid2_tick_size(price, band)
    d_price = Decimal(str(price))
    d_tick = Decimal(str(tick))

    num_ticks = (d_price / d_tick).to_integral_value(rounding=rounding_mode)
    rounded_price = float(num_ticks * d_tick)

    # Recalculate tick at boundary in case crossing price threshold changed tick tier
    new_tick = get_mifid2_tick_size(rounded_price, band)
    if new_tick != tick:
        d_new_tick = Decimal(str(new_tick))
        num_ticks = (Decimal(str(rounded_price)) / d_new_tick).to_integral_value(rounding=rounding_mode)
        rounded_price = float(num_ticks * d_new_tick)
        tick = new_tick

    decimals = abs(Decimal(str(tick)).as_tuple().exponent)
    return round(rounded_price, decimals)


def round_to_tick_size(price: float, liquidity_band: int = 5, side: str = "NEAREST") -> float:
    """Backward-compatible wrapper around `round_to_valid_tick`."""
    return round_to_valid_tick(price=price, side=side, band=liquidity_band)


def calculate_leg_commission(notional_nok: float, profile: BrokerFeeProfile) -> float:
    """Calculate single-leg broker commission in NOK with minimum floor."""
    if notional_nok <= 0:
        return 0.0
    return max(notional_nok * profile.commission_pct, profile.min_commission_nok)


def evaluate_round_trip_friction(
    shares: int,
    entry_price: float,
    exit_price: float,
    holding_days: int,
    profile: BrokerFeeProfile,
    max_cost_drag_ratio: float = 0.20,
) -> TradeFrictionSummary:
    """Calculate net P&L and check whether total friction stays under the 20% Cost Drag ceiling."""
    entry_notional = shares * entry_price
    exit_notional = shares * exit_price
    gross_pnl = exit_notional - entry_notional

    entry_comm = calculate_leg_commission(entry_notional, profile)
    exit_comm = calculate_leg_commission(exit_notional, profile)

    one_way_slippage_pct = profile.half_spread_pct + profile.extra_slippage_pct
    spread_slippage_nok = (entry_notional + exit_notional) * one_way_slippage_pct

    avg_notional = 0.5 * (entry_notional + exit_notional)
    custody_nok = avg_notional * (profile.custody_fee_annual_pct / 365.0) * max(holding_days, 1)

    total_cost = entry_comm + exit_comm + spread_slippage_nok + custody_nok
    net_pnl = gross_pnl - total_cost

    if gross_pnl > 0:
        cost_drag_ratio = total_cost / gross_pnl
        passes_gate = cost_drag_ratio <= max_cost_drag_ratio
    else:
        cost_drag_ratio = None
        passes_gate = False

    return TradeFrictionSummary(
        entry_notional_nok=round(entry_notional, 2),
        exit_notional_nok=round(exit_notional, 2),
        gross_pnl_nok=round(gross_pnl, 2),
        entry_commission_nok=round(entry_comm, 2),
        exit_commission_nok=round(exit_comm, 2),
        spread_and_slippage_nok=round(spread_slippage_nok, 2),
        custody_fee_nok=round(custody_nok, 2),
        total_cost_nok=round(total_cost, 2),
        net_pnl_nok=round(net_pnl, 2),
        cost_drag_pct_of_gross=round(cost_drag_ratio, 4) if cost_drag_ratio is not None else None,
        passes_cost_drag_gate=passes_gate,
    )
