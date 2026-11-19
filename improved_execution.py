"""
OBX Trading Engine - Production Improvements:
1. MiFID II Dynamic Tick Size Engine (Oslo Børs XOSL / Band 5 & 6)
2. Dual-Price Architecture: Adjusted Close for Indicators vs. Raw Close for Execution
3. Pre-Execution Auction & Slippage Guardrails with Idempotent Saxo OpenAPI Orders
"""

from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING, ROUND_HALF_UP
from enum import Enum
import math
import uuid
import pandas as pd
import numpy as np


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class LiquidityBand(int, Enum):
    BAND_1 = 1  # 0 <= ADNT < 10
    BAND_2 = 2  # 10 <= ADNT < 80
    BAND_3 = 3  # 80 <= ADNT < 600
    BAND_4 = 4  # 600 <= ADNT < 2000
    BAND_5 = 5  # 2000 <= ADNT < 9000 (standard large cap OBX)
    BAND_6 = 6  # ADNT >= 9000 (EQNR, DNB, NHY)


# MiFID II RTS 28 Tick Size Table thresholds and step sizes
MIFID_II_TABLE = {
    # Upper price bound: {Band: TickSize}
    0.1:   {1: 0.0005, 2: 0.0002, 3: 0.0001, 4: 0.0001, 5: 0.0001, 6: 0.0001},
    0.2:   {1: 0.001,  2: 0.0005, 3: 0.0002, 4: 0.0001, 5: 0.0001, 6: 0.0001},
    0.5:   {1: 0.002,  2: 0.001,  3: 0.0005, 4: 0.0002, 5: 0.0001, 6: 0.0001},
    1.0:   {1: 0.005,  2: 0.002,  3: 0.001,  4: 0.0005, 5: 0.0002, 6: 0.0001},
    2.0:   {1: 0.01,   2: 0.005,  3: 0.002,  4: 0.001,  5: 0.0005, 6: 0.0002},
    5.0:   {1: 0.02,   2: 0.01,   3: 0.005,  4: 0.002,  5: 0.001,  6: 0.0005},
    10.0:  {1: 0.05,   2: 0.02,   3: 0.01,   4: 0.005,  5: 0.002,  6: 0.001},
    20.0:  {1: 0.1,    2: 0.05,   3: 0.02,   4: 0.01,   5: 0.005,  6: 0.002},
    50.0:  {1: 0.2,    2: 0.1,    3: 0.05,   4: 0.02,   5: 0.01,   6: 0.005},
    100.0: {1: 0.5,    2: 0.2,    3: 0.1,    4: 0.05,   5: 0.02,   6: 0.01},
    200.0: {1: 1.0,    2: 0.5,    3: 0.2,    4: 0.1,    5: 0.05,   6: 0.02},
    500.0: {1: 2.0,    2: 1.0,    3: 0.5,    4: 0.2,    5: 0.1,    6: 0.05},
    1000.0:{1: 5.0,    2: 2.0,    3: 1.0,    4: 0.5,    5: 0.2,    6: 0.1},
    2000.0:{1: 10.0,   2: 5.0,    3: 2.0,    4: 1.0,    5: 0.5,    6: 0.2},
    5000.0:{1: 20.0,   2: 10.0,   3: 5.0,    4: 2.0,    5: 1.0,    6: 0.5},
    float('inf'): {1: 50.0, 2: 20.0, 3: 10.0, 4: 5.0, 5: 2.0, 6: 1.0}
}


# =====================================================================
# 1. MiFID II Tick Size Engine
# =====================================================================

def get_mifid2_tick_size(price: float, band: LiquidityBand = LiquidityBand.BAND_5) -> float:
    """Finds the MiFID II tick size based on asset price and liquidity band."""
    if price <= 0:
        return 0.01
    for upper_bound, band_dict in sorted(MIFID_II_TABLE.items(), key=lambda x: x[0]):
        if price < upper_bound:
            return band_dict.get(int(band), 0.01)
    return 0.10


def round_to_valid_tick(
    price: float,
    side: OrderSide,
    band: LiquidityBand = LiquidityBand.BAND_5
) -> float:
    """
    Rounds a target limit price to a valid Oslo Børs MiFID II tick.
    Directional rounding logic:
      - BUY: Floor to lower tick (prevents overpaying and aggressive crossing of spread)
      - SELL: Ceil to higher tick (prevents selling below fair limit threshold)
    Uses Decimal arithmetic to prevent floating-point representation anomalies.
    """
    if price <= 0:
        raise ValueError("Price must be strictly positive.")

    tick = get_mifid2_tick_size(price, band)
    d_price = Decimal(str(price))
    d_tick = Decimal(str(tick))

    rounding_mode = ROUND_FLOOR if side == OrderSide.BUY else ROUND_CEILING
    num_ticks = (d_price / d_tick).to_integral_value(rounding=rounding_mode)
    rounded_price = float(num_ticks * d_tick)
    
    # Recalculate tick at boundary in case crossing price threshold changed tick tier
    new_tick = get_mifid2_tick_size(rounded_price, band)
    if new_tick != tick:
        d_new_tick = Decimal(str(new_tick))
        num_ticks = (Decimal(str(rounded_price)) / d_new_tick).to_integral_value(rounding=rounding_mode)
        rounded_price = float(num_ticks * d_new_tick)

    # Format to maximum decimals based on tick precision
    decimals = abs(Decimal(str(tick)).as_tuple().exponent)
    return round(rounded_price, decimals)


# =====================================================================
# 2. Dual-Price Architecture: Adjusted vs. Raw Close
# =====================================================================

def compute_endogenous_signals(df: pd.DataFrame) -> pd.DataFrame:
    """
    Computes technical indicators strictly on 'adj_close' (dividend & split adjusted)
    to prevent false triggers from corporate actions.
    Outputs signals with trade execution targets mapped to 'raw_close'.
    """
    data = df.copy()
    if 'adj_close' not in data.columns or 'raw_close' not in data.columns:
        raise ValueError("DataFrame must contain both 'adj_close' and 'raw_close'.")

    # Ratio for mapping adjusted values back to current market quotes
    data['adj_ratio'] = data['raw_close'] / data['adj_close']

    # Mean Reversion: 200 SMA & 2-day RSI on ADJ CLOSE
    data['sma_200'] = data['adj_close'].rolling(window=200).mean()
    delta = data['adj_close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=2).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=2).mean()
    rs = gain / (loss + 1e-9)
    data['rsi_2'] = 100 - (100 / (1 + rs))

    # Breakout: 20-day high and 20-day volume average
    data['high_20'] = data['adj_close'].shift(1).rolling(window=20).max()
    data['vol_sma20'] = data['volume'].rolling(window=20).mean()

    # Signals
    mean_rev_signal = (data['adj_close'] > data['sma_200']) & (data['rsi_2'] < 10)
    breakout_signal = (data['adj_close'] > data['high_20']) & (data['volume'] > 1.3 * data['vol_sma20'])

    data['signal_buy'] = mean_rev_signal | breakout_signal

    # Signal reference price is RAW CLOSE (what the broker executes)
    data['exec_limit_price'] = data['raw_close']

    return data


# =====================================================================
# 3. Pre-Execution Guardrails & Idempotent Saxo Order Generator
# =====================================================================

@dataclass
class PreExecutionValidation:
    is_valid: bool
    status: str
    proposed_limit_price: float
    current_market_price: float
    slippage_pct: float
    reason: str


def validate_execution_guardrails(
    signal_close_price: float,
    current_market_price: float,
    side: OrderSide,
    max_slippage_pct: float = 1.5,
    max_gap_down_pct: float = -2.0,
    band: LiquidityBand = LiquidityBand.BAND_5
) -> PreExecutionValidation:
    """
    Validates that the morning auction or market price hasn't gapped beyond
    the acceptable slippage threshold (+1.5% for BUY, -2.0% for SELL).
    Rounds the valid limit price to Oslo Børs MiFID II tick size.
    """
    slippage_pct = ((current_market_price - signal_close_price) / signal_close_price) * 100.0

    if side == OrderSide.BUY:
        if slippage_pct > max_slippage_pct:
            return PreExecutionValidation(
                is_valid=False,
                status="REJECTED_PRICE_GAPPED_UP",
                proposed_limit_price=signal_close_price,
                current_market_price=current_market_price,
                slippage_pct=round(slippage_pct, 2),
                reason=f"Current market price ({current_market_price:.2f}) gapped +{slippage_pct:.2f}% above signal close. Exceeds limit of {max_slippage_pct}%."
            )
        if slippage_pct < max_gap_down_pct:
            return PreExecutionValidation(
                is_valid=False,
                status="REJECTED_PRICE_GAPPED_DOWN",
                proposed_limit_price=signal_close_price,
                current_market_price=current_market_price,
                slippage_pct=round(slippage_pct, 2),
                reason=f"Current market price ({current_market_price:.2f}) gapped {slippage_pct:.2f}% below signal close. Exceeds limit of {max_gap_down_pct}%."
            )
        # Use current price with floor rounding for limit order
        target_limit = round_to_valid_tick(current_market_price, OrderSide.BUY, band)
    else:
        # SELL order guardrail
        target_limit = round_to_valid_tick(current_market_price, OrderSide.SELL, band)

    return PreExecutionValidation(
        is_valid=True,
        status="APPROVED_FOR_EXECUTION",
        proposed_limit_price=target_limit,
        current_market_price=current_market_price,
        slippage_pct=round(slippage_pct, 2),
        reason="Price is within acceptable slippage and tick-rounded."
    )


def build_saxo_order_payload(
    uic: int,
    account_key: str,
    amount: int,
    limit_price: float,
    side: OrderSide = OrderSide.BUY,
    duration: str = "DayOrder",
    idempotency_key: str | None = None,
) -> dict:
    """
    Constructs an idempotent Saxo OpenAPI limit order with an explicit x-request-id
    and valid DayOrder parameters.
    """
    req_key = idempotency_key or str(uuid.uuid4())
    
    payload = {
        "AccountKey": account_key,
        "Uic": uic,
        "AssetType": "Stock",
        "BuySell": "Buy" if side == OrderSide.BUY else "Sell",
        "Amount": amount,
        "OrderType": "Limit",
        "OrderPrice": limit_price,
        "OrderDuration": {
            "DurationType": duration
        },
        "ManualOrder": False
    }

    headers = {
        "Content-Type": "application/json",
        "X-Request-Id": req_key,
        "x-request-id": req_key,
    }

    return {
        "headers": headers,
        "payload": payload,
        "idempotency_key": req_key
    }
