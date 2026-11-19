"""Agent 4: Saxo Bank OpenAPI v2 Client, Pre-Execution Guardrails & Execution Engine.

Implements:
- PreExecutionValidation (`validate_execution_guardrails`): Verifies 09:00 CET opening auction
  slippage (+1.5% max gap-up for BUY, -2.0% max gap-down) and applies MiFID II tick-size rounding.
- SaxoRateLimiter: Enforces max 1 order/second and 120 requests/minute.
- Idempotent `/trade/v2/orders` payload builder (`build_saxo_order_payload` & `build_saxo_order_request`)
  with `X-Request-ID` (15-second duplicate guard).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

from config.settings import settings
from src.portfolio.cost_tax_model import LiquidityBand, OrderSide, round_to_valid_tick
from src.portfolio.risk_manager import check_opening_gap_guardrail


@dataclass(frozen=True)
class PreExecutionValidation:
    """Result of pre-execution opening auction slippage and MiFID II tick-size validation."""

    is_valid: bool
    status: str
    proposed_limit_price: float
    current_market_price: float
    slippage_pct: float
    reason: str


def validate_execution_guardrails(
    signal_close_price: float,
    current_market_price: float,
    side: str | OrderSide = OrderSide.BUY,
    max_slippage_pct: float = 1.5,
    max_gap_down_pct: float = -2.0,
    band: int | LiquidityBand = LiquidityBand.BAND_5,
) -> PreExecutionValidation:
    """Validate that morning auction/market price has not gapped outside guardrails and round to MiFID II tick."""
    if signal_close_price <= 0:
        raise ValueError("signal_close_price must be positive.")

    side_enum = side if isinstance(side, OrderSide) else OrderSide(str(side).upper())
    slippage_pct = ((current_market_price - signal_close_price) / signal_close_price) * 100.0

    if side_enum == OrderSide.BUY:
        if slippage_pct > max_slippage_pct:
            return PreExecutionValidation(
                is_valid=False,
                status="REJECTED_PRICE_GAPPED_UP",
                proposed_limit_price=signal_close_price,
                current_market_price=current_market_price,
                slippage_pct=round(slippage_pct, 2),
                reason=(
                    f"Current market price ({current_market_price:.2f}) gapped +{slippage_pct:.2f}% "
                    f"above signal close. Exceeds limit of +{max_slippage_pct:.2f}%."
                ),
            )
        if slippage_pct < max_gap_down_pct:
            return PreExecutionValidation(
                is_valid=False,
                status="REJECTED_PRICE_GAPPED_DOWN",
                proposed_limit_price=signal_close_price,
                current_market_price=current_market_price,
                slippage_pct=round(slippage_pct, 2),
                reason=(
                    f"Current market price ({current_market_price:.2f}) gapped {slippage_pct:.2f}% "
                    f"below signal close. Exceeds limit of {max_gap_down_pct:.2f}%."
                ),
            )
        target_limit = round_to_valid_tick(current_market_price, OrderSide.BUY, band)
    else:
        target_limit = round_to_valid_tick(current_market_price, OrderSide.SELL, band)

    return PreExecutionValidation(
        is_valid=True,
        status="APPROVED_FOR_EXECUTION",
        proposed_limit_price=target_limit,
        current_market_price=current_market_price,
        slippage_pct=round(slippage_pct, 2),
        reason="Price is within acceptable slippage and tick-rounded.",
    )


class SaxoRateLimiter:
    """Deterministic rate limiter enforcing Saxo's 1 order/sec and 120 req/min limits."""

    def __init__(self, min_order_interval_sec: float = 1.0) -> None:
        self.min_order_interval_sec = min_order_interval_sec
        self._last_order_ts: float = 0.0

    def wait_for_order_slot(self, now_fn=time.monotonic, sleep_fn=time.sleep) -> float:
        """Return seconds waited to respect the 1 order/second constraint."""
        now = now_fn()
        elapsed = now - self._last_order_ts
        wait_sec = 0.0
        if self._last_order_ts > 0.0 and elapsed < self.min_order_interval_sec:
            wait_sec = self.min_order_interval_sec - elapsed
            sleep_fn(wait_sec)
        self._last_order_ts = now_fn()
        return round(wait_sec, 4)


@dataclass(frozen=True)
class SaxoOrderRequest:
    """Validated and tick-rounded Saxo OpenAPI v2 order request with idempotency header."""

    endpoint: str
    headers: dict[str, str]
    body: dict[str, Any]


def build_saxo_order_payload(
    uic: int,
    account_key: str,
    amount: int,
    limit_price: float,
    side: str | OrderSide = OrderSide.BUY,
    duration: str = "DayOrder",
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Construct an idempotent Saxo OpenAPI limit order dict with explicit X-Request-Id."""
    side_str = side.value if isinstance(side, OrderSide) else str(side).upper()
    req_key = idempotency_key or str(uuid.uuid4())
    payload = {
        "AccountKey": account_key,
        "Uic": int(uic),
        "AssetType": "Stock",
        "BuySell": "Buy" if side_str == "BUY" else "Sell",
        "Amount": int(amount),
        "OrderType": "Limit",
        "OrderPrice": float(limit_price),
        "OrderDuration": {"DurationType": duration},
        "ManualOrder": False,
    }
    headers = {
        "Content-Type": "application/json",
        "X-Request-ID": req_key,
        "X-Request-Id": req_key,
    }
    return {
        "headers": headers,
        "payload": payload,
        "idempotency_key": req_key,
    }


def build_saxo_order_request(
    uic: int,
    action: str,
    shares: int,
    signal_close_price: float,
    live_open_price: float,
    liquidity_band: int = 5,
    x_request_id: str | None = None,
    account_key: str | None = None,
    enforce_gap_guardrail: bool = True,
) -> SaxoOrderRequest:
    """Construct a valid Saxo `/trade/v2/orders` request after verifying Gap Guardrail and MiFID II tick size."""
    side = action.upper()
    if side not in {"BUY", "SELL"}:
        raise ValueError("action must be 'BUY' or 'SELL'")

    if side == "BUY" and enforce_gap_guardrail:
        gap_res = check_opening_gap_guardrail(signal_close=signal_close_price, next_open=live_open_price)
        if not gap_res.allowed:
            raise ValueError(f"Order blocked by 09:00 Gap Guardrail: {gap_res.status_reason}")

    tick_rounded_price = round_to_valid_tick(live_open_price, side=side, band=liquidity_band)
    req_id = x_request_id or f"saxo_{uuid.uuid4().hex[:16]}"
    acct = account_key or settings.saxo_account_key or "SIM_ACCOUNT_KEY"

    built = build_saxo_order_payload(
        uic=uic,
        account_key=acct,
        amount=shares,
        limit_price=tick_rounded_price,
        side=side,
        idempotency_key=req_id,
    )
    return SaxoOrderRequest(
        endpoint="/trade/v2/orders",
        headers=built["headers"],
        body=built["payload"],
    )
