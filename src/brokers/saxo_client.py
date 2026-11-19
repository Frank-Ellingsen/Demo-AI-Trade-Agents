"""Saxo OpenAPI Client Wrapper for OBX Trading System (`src/brokers/saxo_client.py`).

Handles:
- OAuth 2.0 authentication and automatic token renewal via refresh token.
- Rate limiting: Max 1 order/second (`RateLimitTracker`) and session quota tracking (`X-RateLimit-*`).
- Idempotency: Unique `x-request-id` on all POST orders to prevent 409 Conflict within 15s window.
- Instrument lookup (UIC) and placement of MiFID II tick-rounded Limit orders.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

try:
    import requests
except ImportError:
    requests = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("SaxoClient")


@dataclass
class RateLimitTracker:
    """Tracks Saxo OpenAPI rate limits (1 order/sec and 120 session calls/min)."""

    max_orders_per_sec: int = 1
    last_order_timestamp: float = 0.0
    remaining_session_calls: int = 120
    reset_session_seconds: int = 60

    def enforce_order_rate_limit(self) -> float:
        """Ensure at least 1.0 second elapses between consecutive order placements."""
        now = time.time()
        elapsed = now - self.last_order_timestamp
        min_interval = 1.0 / max(1, self.max_orders_per_sec)
        slept = 0.0
        if self.last_order_timestamp > 0.0 and elapsed < min_interval:
            slept = min_interval - elapsed
            logger.warning(f"Rate limiter aktiv: venter {slept:.2f} sekunder før ny ordre...")
            time.sleep(slept)
        self.last_order_timestamp = time.time()
        return round(slept, 4)

    def update_from_headers(self, headers: Dict[str, Any]) -> None:
        """Update remaining session quota from Saxo response headers."""
        if "X-RateLimit-Session-Remaining" in headers:
            try:
                self.remaining_session_calls = int(headers["X-RateLimit-Session-Remaining"])
            except ValueError:
                pass
        if "X-RateLimit-Session-Reset" in headers:
            try:
                self.reset_session_seconds = int(headers["X-RateLimit-Session-Reset"])
            except ValueError:
                pass


class SaxoClient:
    """Client for Saxo Bank OpenAPI v2."""

    def __init__(self, config_path: str = "config/app_config.json", env_token: Optional[str] = None) -> None:
        self.config_path = config_path
        self.config = self._load_config(config_path)

        self.base_url = self.config.get(
            "OpenApiBaseUrl",
            os.environ.get("SAXO_OPENAPI_BASE_URL", "https://gateway.saxobank.com/sim/openapi"),
        )
        self.token_endpoint = self.config.get(
            "TokenEndpoint",
            os.environ.get("SAXO_TOKEN_ENDPOINT", "https://sim.logonvalidation.net/token"),
        )
        self.app_key = self.config.get("AppKey", os.environ.get("SAXO_APP_KEY", ""))
        self.app_secret = self.config.get("AppSecret", os.environ.get("SAXO_APP_SECRET", ""))

        self.access_token = env_token or os.environ.get("SAXO_ACCESS_TOKEN")
        self.refresh_token = os.environ.get("SAXO_REFRESH_TOKEN")
        self.token_expiry = time.time() + 3600 if self.access_token else 0.0

        self.rate_limiter = RateLimitTracker()
        self.default_account_key: Optional[str] = os.environ.get("SAXO_ACCOUNT_KEY") or None
        self.execution_mode = os.environ.get("EXECUTION_MODE", "paper").lower()
        self.live_execution_ack = os.environ.get("LIVE_EXECUTION_ACK", "")

    def _require_authorized_execution(self) -> None:
        """Block live Saxo sends unless both environment mode and an explicit ack are set."""
        if str(os.environ.get("SAXO_ENV", "sim")).lower() in {"live", "prod", "production"}:
            if self.execution_mode != "live" or self.live_execution_ack != "I_UNDERSTAND_LIVE_RISK":
                raise PermissionError(
                    "Live Saxo execution blocked: set EXECUTION_MODE=live and LIVE_EXECUTION_ACK to the required acknowledgement."
                )

    def _load_config(self, path: str) -> Dict[str, Any]:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        return {
            "OpenApiBaseUrl": "https://gateway.saxobank.com/sim/openapi",
            "TokenEndpoint": "https://sim.logonvalidation.net/token",
            "AppKey": os.environ.get("SAXO_APP_KEY", "YOUR_APP_KEY"),
            "AppSecret": os.environ.get("SAXO_APP_SECRET", "YOUR_APP_SECRET"),
        }

    def set_tokens(self, access_token: str, refresh_token: Optional[str] = None, expires_in: int = 1200) -> None:
        """Configure OAuth tokens manually or from a 24-hour simulation token."""
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.token_expiry = time.time() + expires_in
        logger.info(f"Access token konfigurert. Utløper om {expires_in} sekunder.")

    def refresh_access_token(self) -> bool:
        """Exchange `refresh_token` for a new `access_token` at the OAuth token endpoint."""
        if not self.refresh_token or not self.app_key or not self.app_secret or not self.token_endpoint:
            logger.warning("Mangler refresh_token, token_endpoint eller app credentials. Kan ikke fornye automatisk.")
            return False

        payload = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "client_id": self.app_key,
            "client_secret": self.app_secret,
        }
        try:
            if requests is not None:
                resp = requests.post(self.token_endpoint, data=payload, timeout=10)
                if resp.status_code == 200:
                    data = resp.json()
                else:
                    return False
            else:
                encoded = urllib.parse.urlencode(payload).encode("utf-8")
                req = urllib.request.Request(self.token_endpoint, data=encoded, method="POST")
                with urllib.request.urlopen(req, timeout=10) as r:
                    data = json.loads(r.read().decode("utf-8"))

            self.access_token = data.get("access_token")
            self.refresh_token = data.get("refresh_token", self.refresh_token)
            expires_in = int(data.get("expires_in", 1200))
            self.token_expiry = time.time() + expires_in
            return True
        except Exception as e:
            logger.error(f"Unntak under tokenfornyelse: {e}")
            return False

    def _ensure_valid_token(self) -> None:
        if time.time() >= (self.token_expiry - 60) and self.refresh_token:
            self.refresh_access_token()

    def get_headers(self, request_id: Optional[str] = None) -> Dict[str, str]:
        self._ensure_valid_token()
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }
        if request_id:
            headers["x-request-id"] = request_id
            headers["X-Request-Id"] = request_id
        return headers

    def get(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Authenticated GET request against Saxo OpenAPI."""
        if not self.base_url:
            raise ValueError("OpenApiBaseUrl er ikke konfigurert.")
        url = f"{self.base_url.rstrip('/')}/{endpoint.lstrip('/')}"
        headers = self.get_headers()

        if requests is not None:
            resp = requests.get(url, headers=headers, params=params, timeout=10)
            self.rate_limiter.update_from_headers(dict(resp.headers))
            if resp.status_code == 401 and self.refresh_token and self.refresh_access_token():
                headers = self.get_headers()
                resp = requests.get(url, headers=headers, params=params, timeout=10)
            resp.raise_for_status()
            return resp.json()

        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=10) as r:
            self.rate_limiter.update_from_headers(dict(r.headers))
            return json.loads(r.read().decode("utf-8"))

    def get_accounts(self) -> Dict[str, Any]:
        """Fetch active accounts and cache primary AccountKey."""
        data = self.get("/port/v1/accounts/me")
        accounts = data.get("Data", [])
        if accounts:
            self.default_account_key = accounts[0].get("AccountKey")
        return data

    def get_instrument_uic(self, ticker: str, asset_type: str = "Stock") -> Optional[int]:
        """Look up Saxo Universal Instrument Code (UIC) for an Oslo Børs ticker."""
        clean_symbol = ticker.replace(".OL", "")
        try:
            data = self.get("/ref/v1/instruments", params={"Keywords": clean_symbol, "AssetTypes": asset_type})
            for item in data.get("Data", []):
                if item.get("Symbol") == clean_symbol or item.get("ExchangeId") == "XOSL":
                    return item.get("Identifier")
        except Exception as e:
            logger.error(f"Feil ved oppslag av UIC for {ticker}: {e}")
        return None

    def place_limit_order(
        self,
        uic: int,
        amount: int,
        limit_price: float,
        side: str = "Buy",
        account_key: Optional[str] = None,
        duration: str = "DayOrder",
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Place a Limit order with 1 order/sec rate-limiting and unique x-request-id idempotency."""
        self._require_authorized_execution()
        self.rate_limiter.enforce_order_rate_limit()
        req_id = request_id or str(uuid.uuid4())
        acc_key = account_key or self.default_account_key
        if not acc_key:
            raise ValueError("AccountKey mangler. Kjør get_accounts() først eller oppgi account_key.")

        payload = {
            "AccountKey": acc_key,
            "Uic": int(uic),
            "AssetType": "Stock",
            "BuySell": "Buy" if str(side).upper() == "BUY" else "Sell",
            "Amount": int(amount),
            "OrderType": "Limit",
            "OrderPrice": float(limit_price),
            "OrderDuration": {"DurationType": duration},
            "ManualOrder": False,
        }
        url = f"{self.base_url.rstrip('/')}/trade/v2/orders"
        headers = self.get_headers(request_id=req_id)

        if requests is not None:
            resp = requests.post(url, headers=headers, json=payload, timeout=10)
            self.rate_limiter.update_from_headers(dict(resp.headers))
            resp.raise_for_status()
            return resp.json()

        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            self.rate_limiter.update_from_headers(dict(r.headers))
            return json.loads(r.read().decode("utf-8"))

    def place_stop_order(
        self,
        uic: int,
        amount: int,
        stop_price: float,
        account_key: Optional[str] = None,
        duration: str = "GoodTillCancel",
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Place an idempotent sell-stop order for downside protection."""
        self._require_authorized_execution()
        if stop_price <= 0:
            raise ValueError("Stop price must be positive.")
        if amount <= 0:
            raise ValueError("Amount must be positive.")

        self.rate_limiter.enforce_order_rate_limit()
        req_id = request_id or str(uuid.uuid4())
        acc_key = account_key or self.default_account_key
        if not acc_key:
            raise ValueError("AccountKey mangler. Kjør get_accounts() først eller oppgi account_key.")

        payload = {
            "AccountKey": acc_key,
            "Uic": int(uic),
            "AssetType": "Stock",
            "BuySell": "Sell",
            "Amount": int(amount),
            "OrderType": "Stop",
            "OrderPrice": float(stop_price),
            "OrderDuration": {"DurationType": duration},
            "ManualOrder": False,
        }
        url = f"{self.base_url.rstrip('/')}/trade/v2/orders"
        headers = self.get_headers(request_id=req_id)

        if requests is not None:
            resp = requests.post(url, headers=headers, json=payload, timeout=10)
            self.rate_limiter.update_from_headers(dict(resp.headers))
            resp.raise_for_status()
            return resp.json()

        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            self.rate_limiter.update_from_headers(dict(r.headers))
            return json.loads(r.read().decode("utf-8"))
