"""Alpaca S&P 500 Execution Client for Multi-Agent Trading System (`src/brokers/alpaca_client.py`).

Handles:
- REST API v2 integration against Alpaca Paper Trading (`https://paper-api.alpaca.markets/v2`) or Live.
- Native Bracket Orders (`order_class="bracket"`: Entry Limit/Market + OCO Take-Profit & Stop-Loss)
  with optional `client_order_id` idempotency.
- Account equity, buying power, cash buffer, and open positions lookup.
- Emergency Circuit Breaker (`cancel_all_orders`).
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

try:
    import requests
except ImportError:
    requests = None

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("AlpacaExecution")


def build_alpaca_bracket_payload(
    symbol: str,
    qty: int,
    side: str = "buy",
    limit_price: Optional[float] = None,
    take_profit_price: Optional[float] = None,
    stop_loss_price: Optional[float] = None,
    time_in_force: str = "day",
    client_order_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Construct a validated Alpaca v2 Bracket Order payload (Entry + OCO Take-Profit & Stop-Loss)."""
    payload: Dict[str, Any] = {
        "symbol": symbol.upper().strip(),
        "qty": str(int(qty)),
        "side": side.lower().strip(),
        "type": "limit" if limit_price is not None else "market",
        "time_in_force": time_in_force,
        "order_class": "bracket",
    }
    if client_order_id:
        payload["client_order_id"] = client_order_id
    if limit_price is not None:
        payload["limit_price"] = str(round(float(limit_price), 2))
    if take_profit_price is not None:
        payload["take_profit"] = {"limit_price": str(round(float(take_profit_price), 2))}
    if stop_loss_price is not None:
        payload["stop_loss"] = {"stop_price": str(round(float(stop_loss_price), 2))}
    return payload


class AlpacaClient:
    """Execution client for US equities (S&P 500) via Alpaca Markets REST API v2."""

    def __init__(
        self,
        endpoint: Optional[str] = None,
        api_key: Optional[str] = None,
        secret_key: Optional[str] = None,
    ) -> None:
        raw_ep = (
            endpoint
            or os.getenv("ALPACA_ENDPOINT")
            or os.getenv("alpaca_endpoint")
            or os.getenv("APCA_API_BASE_URL")
            or ""
        ).rstrip("/")
        # Normalize base endpoint so `/v2` is always present if user passed root domain
        if raw_ep and not raw_ep.endswith("/v2"):
            raw_ep = f"{raw_ep}/v2"
        self.endpoint = raw_ep

        self.api_key = (
            api_key
            or os.getenv("ALPACA_API_KEY")
            or os.getenv("alpaca_key")
            or os.getenv("APCA_API_KEY_ID")
        )
        self.secret_key = (
            secret_key
            or os.getenv("ALPACA_SECRET_KEY")
            or os.getenv("alpaca_secret")
            or os.getenv("APCA_API_SECRET_KEY")
        )

        if not self.endpoint:
            raise ValueError("Mangler Alpaca endpoint. Sjekk .env for ALPACA_ENDPOINT.")

        parsed_ep = urlparse(self.endpoint)
        if parsed_ep.scheme != "https" or (parsed_ep.hostname or "").lower() != "paper-api.alpaca.markets":
            raise ValueError(
                f"SIKKERHETSSPERRE: Kun Alpaca Paper Trading (https://paper-api.alpaca.markets/v2) er tillatt. "
                f"Avvist endepunkt: {self.endpoint}"
            )

        if not self.api_key or not self.secret_key:
            raise ValueError("Mangler Alpaca API credentials. Sjekk .env for alpaca_key og alpaca_secret.")

        self.headers = {
            "APCA-API-KEY-ID": self.api_key.strip(),
            "APCA-API-SECRET-KEY": self.secret_key.strip(),
            "Content-Type": "application/json",
        }

    def get_account(self) -> Dict[str, Any]:
        """Fetch account status, equity, buying power, and cash balance."""
        url = f"{self.endpoint}/account"
        if requests is not None:
            resp = requests.get(url, headers=self.headers, timeout=10)
            resp.raise_for_status()
            return resp.json()
        req = urllib.request.Request(url, headers=self.headers, method="GET")
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))

    def get_positions(self) -> List[Dict[str, Any]]:
        """Fetch all open US positions in the Alpaca portfolio."""
        url = f"{self.endpoint}/positions"
        if requests is not None:
            resp = requests.get(url, headers=self.headers, timeout=10)
            resp.raise_for_status()
            return resp.json()
        req = urllib.request.Request(url, headers=self.headers, method="GET")
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))

    def get_orders(self, status: str = "all", limit: int = 15) -> List[Dict[str, Any]]:
        """Fetch recent orders from Alpaca Paper Trading."""
        url = f"{self.endpoint}/orders?status={status}&limit={int(limit)}"
        if requests is not None:
            resp = requests.get(url, headers=self.headers, timeout=10)
            resp.raise_for_status()
            return resp.json()
        req = urllib.request.Request(url, headers=self.headers, method="GET")
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))

    def get_portfolio_snapshot(self) -> Dict[str, Any]:
        """Return normalized account summary, open positions, and recent orders."""
        acct = self.get_account()
        positions = self.get_positions()
        orders = self.get_orders(status="all", limit=15)

        equity = float(acct.get("equity") or 0.0)
        last_equity = float(acct.get("last_equity") or equity)
        cash = float(acct.get("cash") or 0.0)
        buying_power = float(acct.get("buying_power") or cash)
        long_mv = float(acct.get("long_market_value") or 0.0)
        today_pnl = equity - last_equity
        today_pnl_pct = ((today_pnl / last_equity) * 100.0) if last_equity > 0 else 0.0

        norm_positions = []
        for p in positions:
            norm_positions.append({
                "symbol": str(p.get("symbol", "")),
                "qty": float(p.get("qty") or 0),
                "avg_entry_price": float(p.get("avg_entry_price") or 0),
                "current_price": float(p.get("current_price") or 0),
                "market_value": float(p.get("market_value") or 0),
                "change_today_pct": float(p.get("change_today") or 0) * 100.0,
                "unrealized_pl": float(p.get("unrealized_pl") or 0),
                "unrealized_plpc": float(p.get("unrealized_plpc") or 0) * 100.0,
                "asset_class": str(p.get("asset_class") or "us_equity"),
            })

        norm_orders = []
        for o in orders:
            norm_orders.append({
                "id": str(o.get("id") or ""),
                "symbol": str(o.get("symbol") or ""),
                "side": str(o.get("side") or "").upper(),
                "qty": str(o.get("qty") or "0"),
                "order_class": str(o.get("order_class") or "simple"),
                "limit_price": o.get("limit_price"),
                "filled_avg_price": o.get("filled_avg_price"),
                "status": str(o.get("status") or ""),
                "submitted_at": str(o.get("submitted_at") or ""),
            })

        unrealized_pl = sum(float(p.get("unrealized_pl", 0.0)) for p in norm_positions)
        initial_equity = float(os.getenv("ALPACA_INITIAL_EQUITY", "100000.0"))
        realized_pl = (equity - initial_equity) - unrealized_pl

        return {
            "connected": True,
            "endpoint": self.endpoint,
            "account_number": str(acct.get("account_number") or ""),
            "status": str(acct.get("status") or "ACTIVE"),
            "equity": round(equity, 2),
            "last_equity": round(last_equity, 2),
            "cash": round(cash, 2),
            "buying_power": round(buying_power, 2),
            "long_market_value": round(long_mv, 2),
            "today_pnl": round(today_pnl, 2),
            "today_pnl_pct": round(today_pnl_pct, 2),
            "unrealized_pl": round(unrealized_pl, 2),
            "realized_pl": round(realized_pl, 2),
            "positions": norm_positions,
            "recent_orders": norm_orders,
        }

    def place_bracket_order(
        self,
        symbol: str,
        qty: int,
        side: str = "buy",
        limit_price: Optional[float] = None,
        take_profit_price: Optional[float] = None,
        stop_loss_price: Optional[float] = None,
        time_in_force: str = "day",
        client_order_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Place a Bracket Order (Entry + OCO Take-Profit & Stop-Loss) on Alpaca v2."""
        url = f"{self.endpoint}/orders"
        payload = build_alpaca_bracket_payload(
            symbol=symbol,
            qty=qty,
            side=side,
            limit_price=limit_price,
            take_profit_price=take_profit_price,
            stop_loss_price=stop_loss_price,
            time_in_force=time_in_force,
            client_order_id=client_order_id,
        )

        logger.info(
            f"Sender Alpaca bracket-ordre: {side.upper()} {qty} {symbol} "
            f"@ Limit: {limit_price} | TP: {take_profit_price} | SL: {stop_loss_price}"
        )

        if requests is not None:
            resp = requests.post(url, headers=self.headers, json=payload, timeout=10)
            if resp.status_code not in (200, 201):
                logger.error(f"Feil ved innsending av Alpaca-ordre ({resp.status_code}): {resp.text}")
                resp.raise_for_status()
            data = resp.json()
        else:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers=self.headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode("utf-8"))

        logger.info(f"Ordre akseptert av Alpaca! Hovedordre ID: {data.get('id')} (Status: {data.get('status')})")
        return data

    def cancel_all_orders(self) -> List[Dict[str, Any]]:
        """Cancel all active/open orders (Circuit Breaker)."""
        url = f"{self.endpoint}/orders"
        if requests is not None:
            resp = requests.delete(url, headers=self.headers, timeout=10)
            resp.raise_for_status()
            logger.warning("Alle aktive Alpaca-ordrer er kansellert.")
            return resp.json()
        req = urllib.request.Request(url, headers=self.headers, method="DELETE")
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))
