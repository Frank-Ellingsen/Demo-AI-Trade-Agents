"""Stream Execution Bridge: Saxo WebSocket -> Slippage Guardrail -> Limit Order (`stream_execution_bridge.py`).

Connects real-time quotes from `SaxoStreamer` with:
1. Approved HITL orders in `SQLite WAL` (`data/hitl_orders.db`).
2. 09:00 CET Opening Auction & Slippage Guardrail (+1.5% max gap-up for BUY, -2.0% max gap-down).
3. Euronext Oslo Børs MiFID II Tick-Size rounding (`round_to_valid_tick`).
4. Idempotent order dispatch (`X-Request-Id`) via `SaxoClient`.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from alpaca_execution import build_alpaca_bracket_payload
from improved_execution import (
    LiquidityBand,
    OrderSide,
    PreExecutionValidation,
    build_saxo_order_payload,
    round_to_valid_tick,
    validate_execution_guardrails,
)
from src.db.sqlite_store import get_sqlite_conn, init_sqlite_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("StreamExecutionBridge")

# Default Oslo Børs OBX 15 Liquidity Band mapping (Band 6 for ultra-liquid EQNR, DNB, NHY; Band 5 for others)
OBX_LIQUIDITY_BANDS: dict[str, LiquidityBand] = {
    "EQNR.OL": LiquidityBand.BAND_6,
    "DNB.OL": LiquidityBand.BAND_6,
    "NHY.OL": LiquidityBand.BAND_6,
}


class StreamExecutionBridge:
    """Monitors real-time quotes from Saxo WebSocket and executes APPROVED HITL orders
    only if market price satisfies slippage and MiFID II tick-size rules.
    """

    def __init__(
        self,
        db_path: str | Path = "data/hitl_orders.db",
        max_slippage_pct: float = 1.5,
        default_band: LiquidityBand = LiquidityBand.BAND_5,
    ) -> None:
        self.db_path = str(db_path)
        self.max_slippage_pct = max_slippage_pct
        self.default_band = default_band
        self.latest_quotes: Dict[str, Dict[str, Any]] = {}
        init_sqlite_db(self.db_path)

    def update_quote(
        self,
        ticker: str,
        bid: Optional[float],
        ask: Optional[float],
        last: Optional[float] = None,
    ) -> None:
        """Update internal real-time quote buffer from WebSocket stream."""
        mid = round((bid + ask) / 2.0, 4) if (bid is not None and ask is not None) else None
        self.latest_quotes[ticker] = {
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "last": last or ask or mid,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    def get_pending_approved_orders(self) -> List[Dict[str, Any]]:
        """Fetch orders from SQLite WAL that were APPROVED by the user in Telegram/UI."""
        with get_sqlite_conn(self.db_path) as con:
            rows = con.execute(
                """
                SELECT
                    order_id,
                    x_request_id,
                    ticker,
                    COALESCE(action, side, 'BUY') AS side,
                    COALESCE(shares, amount, 1) AS amount,
                    CASE WHEN signal_close_price > 0 THEN signal_close_price ELSE limit_price END AS signal_close_price,
                    limit_price,
                    stop_loss_price,
                    protective_stop_request_id,
                    protective_stop_status,
                    COALESCE(llm_recommendation, 'GO') AS llm_recommendation,
                    COALESCE(earnings_blackout_flag, 0) AS earnings_blackout_flag,
                    COALESCE(gemini_reason, bull_case, '') AS gemini_reason
                FROM pending_orders
                WHERE status = 'APPROVED'
                  AND expires_at IS NOT NULL
                  AND expires_at > ?
                  AND COALESCE(llm_recommendation, 'GO') = 'GO'
                  AND COALESCE(earnings_blackout_flag, 0) = 0;
                """,
                (datetime.now(timezone.utc).isoformat(),),
            ).fetchall()
            return [dict(r) for r in rows]

    def mark_order_status(self, order_id: str, new_status: str, execution_notes: str = "") -> None:
        """Update order status and append execution notes in SQLite WAL."""
        with get_sqlite_conn(self.db_path) as con:
            con.execute(
                """
                UPDATE pending_orders
                SET status = ?,
                    gemini_reason = COALESCE(gemini_reason, '') || ' | ' || ?
                WHERE order_id = ?;
                """,
                (new_status, execution_notes, order_id),
            )

    def mark_protective_stop_status(
        self,
        order_id: str,
        stop_status: str,
        stop_price: float,
        execution_notes: str = "",
    ) -> None:
        """Persist protective-stop state and append the broker/result note."""
        with get_sqlite_conn(self.db_path) as con:
            con.execute(
                """
                UPDATE pending_orders
                SET protective_stop_status = ?,
                    protective_stop_price = ?,
                    protective_stop_request_id =
                        COALESCE(protective_stop_request_id, x_request_id || ':stop'),
                    gemini_reason = COALESCE(gemini_reason, '') || ' | ' || ?
                WHERE order_id = ?;
                """,
                (stop_status, stop_price, execution_notes, order_id),
            )

    def mark_broker_fill(
        self,
        order_id: str,
        broker_order_id: str,
        fill_status: str,
        fill_price: Optional[float] = None,
        fill_qty: Optional[int] = None,
    ) -> None:
        """Persist broker-order identity and fill/submission state in the same SQLite record."""
        with get_sqlite_conn(self.db_path) as con:
            con.execute(
                """
                UPDATE pending_orders
                SET broker_order_id = ?,
                    fill_status = ?,
                    fill_price = COALESCE(?, fill_price),
                    fill_qty = COALESCE(?, fill_qty),
                    reserved_cash_nok = CASE WHEN ? IN ('FILLED', 'SUBMITTED') THEN reserved_cash_nok ELSE 0.0 END
                WHERE order_id = ?;
                """,
                (broker_order_id, fill_status, fill_price, fill_qty, fill_status, order_id),
            )

    def process_incoming_tick(
        self,
        ticker: str,
        market_price: float,
        saxo_client: Any = None,
        alpaca_client: Any = None,
        uic: int = 2115,
    ) -> Optional[Dict[str, Any]]:
        """Triggered on incoming WebSocket tick: validates slippage guardrail and dispatches to Saxo (OBX .OL) or Alpaca (US S&P 500)."""
        approved_orders = self.get_pending_approved_orders()
        matching_orders = [o for o in approved_orders if o["ticker"] == ticker]
        if not matching_orders:
            return None

        is_oslo_bors = ticker.upper().endswith(".OL")
        band = OBX_LIQUIDITY_BANDS.get(ticker, self.default_band)

        for order in matching_orders:
            order_id = order["order_id"]
            side = OrderSide.BUY if str(order["side"]).upper() == "BUY" else OrderSide.SELL
            signal_close = float(order["signal_close_price"] or order["limit_price"])

            validation: PreExecutionValidation = validate_execution_guardrails(
                signal_close_price=signal_close,
                current_market_price=market_price,
                side=side,
                max_slippage_pct=self.max_slippage_pct,
                band=band,
            )

            if not validation.is_valid:
                logger.warning(
                    f"ORDRE {order_id} ({ticker}) AVVIST AV SLIPPAGE-VAKT: "
                    f"Signal: {signal_close:.2f}, Marked: {market_price:.2f} ({validation.status})"
                )
                self.mark_order_status(
                    order_id=order_id,
                    new_status="REJECTED_BY_SLIPPAGE_GUARD",
                    execution_notes=f"Avvist ved åpning: Markedskurs {market_price:.2f} gappet forbi limit {signal_close:.2f}.",
                )
                continue

            # Route US S&P 500 tickers to Alpaca Bracket Order v2, and Oslo Børs (.OL) tickers to Saxo OpenAPI v2
            if not is_oslo_bors:
                us_limit = round(float(market_price), 2)
                sl_raw = order.get("stop_loss_price")
                sl_price = round(float(sl_raw), 2) if sl_raw else round(us_limit * 0.96, 2)
                tp_price = round(us_limit * 1.06, 2)
                alpaca_payload = build_alpaca_bracket_payload(
                    symbol=ticker,
                    qty=int(order["amount"]),
                    side="buy" if side == OrderSide.BUY else "sell",
                    limit_price=us_limit,
                    take_profit_price=tp_price,
                    stop_loss_price=sl_price,
                    client_order_id=order.get("x_request_id") or order_id,
                )
                if alpaca_client is not None:
                    try:
                        alpaca_client.place_bracket_order(
                            symbol=ticker,
                            qty=int(order["amount"]),
                            side="buy" if side == OrderSide.BUY else "sell",
                            limit_price=us_limit,
                            take_profit_price=tp_price,
                            stop_loss_price=sl_price,
                            client_order_id=order.get("x_request_id") or order_id,
                        )
                        self.mark_broker_fill(
                            order_id,
                            broker_order_id=str(order.get("x_request_id") or order_id),
                            fill_status="SUBMITTED",
                            fill_price=us_limit,
                            fill_qty=int(order["amount"]),
                        )
                        self.mark_order_status(order_id, "EXECUTED_IN_ALPACA", f"Sendt til Alpaca @ ${us_limit:.2f}")
                    except Exception as e:
                        logger.error(f"Feil ved innsending til Alpaca: {e}")
                        self.mark_order_status(order_id, "ERROR_ALPACA_API", str(e))
                else:
                    self.mark_order_status(order_id, "STAGED_FOR_ALPACA", f"Klar for Alpaca @ ${us_limit:.2f}")
                return alpaca_payload

            tick_limit_price = validation.proposed_limit_price
            order_payload = build_saxo_order_payload(
                uic=uic,
                account_key="ACC_ACTIVE",
                amount=int(order["amount"]),
                limit_price=tick_limit_price,
                side=side,
                idempotency_key=order.get("x_request_id") or None,
            )

            if saxo_client is not None:
                try:
                    saxo_client.place_limit_order(
                        uic=uic,
                        amount=int(order["amount"]),
                        limit_price=tick_limit_price,
                        side="Buy" if side == OrderSide.BUY else "Sell",
                        request_id=order_payload["idempotency_key"],
                    )
                    self.mark_broker_fill(
                        order_id,
                        broker_order_id=order_payload["idempotency_key"],
                        fill_status="SUBMITTED",
                        fill_price=tick_limit_price,
                        fill_qty=int(order["amount"]),
                    )
                    self.mark_order_status(order_id, "EXECUTED_IN_MARKET", f"Sendt til Saxo @ {tick_limit_price:.2f}")
                    if side == OrderSide.BUY:
                        stop_price = order.get("stop_loss_price")
                        try:
                            stop_price = float(stop_price) if stop_price is not None else 0.0
                        except (TypeError, ValueError):
                            stop_price = 0.0

                        stop_price = round_to_valid_tick(stop_price, OrderSide.SELL, band) if stop_price > 0 else 0.0
                        if stop_price <= 0 or stop_price >= tick_limit_price:
                            self.mark_order_status(
                                order_id,
                                "EXECUTED_STOP_SETUP_FAILED",
                                "Beskyttende stop-loss mangler eller er ugyldig.",
                            )
                            self.mark_protective_stop_status(
                                order_id,
                                "BLOCKED_INVALID_STOP",
                                stop_price,
                                "Beskyttende stopploss avvist: stopprisen må være positiv og under inngangskursen.",
                            )
                            logger.error("ORDRE %s: mangler gyldig beskyttende stop-loss.", order_id)
                        elif order.get("protective_stop_status") == "SUBMITTED":
                            logger.info("ORDRE %s har allerede en innsending av beskyttende stop-loss.", order_id)
                        elif hasattr(saxo_client, "place_stop_order"):
                            stop_request_id = f"{order_payload['idempotency_key']}:stop"
                            try:
                                saxo_client.place_stop_order(
                                    uic=uic,
                                    amount=int(order["amount"]),
                                    stop_price=stop_price,
                                    account_key=saxo_client.default_account_key
                                    if hasattr(saxo_client, "default_account_key")
                                    else None,
                                    request_id=stop_request_id,
                                )
                                self.mark_protective_stop_status(
                                    order_id,
                                    "SUBMITTED",
                                    stop_price,
                                    f"Beskyttende stop-loss sendt til Saxo @ {stop_price:.2f}",
                                )
                            except Exception as stop_error:
                                logger.error("Feil ved innsending av stop-loss for %s: %s", order_id, stop_error)
                                self.mark_order_status(
                                    order_id,
                                    "EXECUTED_STOP_SETUP_FAILED",
                                    f"Stop-loss kunne ikke sendes: {stop_error}",
                                )
                                self.mark_protective_stop_status(
                                    order_id,
                                    "ERROR_SAXO_API",
                                    stop_price,
                                    str(stop_error),
                                )
                        else:
                            self.mark_order_status(
                                order_id,
                                "EXECUTED_STOP_SETUP_FAILED",
                                "Saxo-klienten støtter ikke innsending av stop-loss.",
                            )
                            self.mark_protective_stop_status(
                                order_id,
                                "NOT_SUPPORTED",
                                stop_price,
                                "Saxo-klienten støtter ikke innsending av stop-loss.",
                            )
                except Exception as e:
                    logger.error(f"Feil ved innsending til Saxo: {e}")
                    self.mark_order_status(order_id, "ERROR_SAXO_API", str(e))
            else:
                self.mark_order_status(order_id, "STAGED_FOR_SAXO", f"Klar for innsending @ {tick_limit_price:.2f}")

            return order_payload

        return None
