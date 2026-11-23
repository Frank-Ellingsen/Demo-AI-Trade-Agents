#!/usr/bin/env python3
"""
ALPACA PAPER LIVE RUNNER & HYBRID MARKET DATA SERVER
====================================================
Kjerne-server (FastAPI + WebSocket) for AI Trading Cockpit mot Alpaca Paper v2 og Massive.com.

Hovedoppgaver:
  1. Direkte sanntidssynkronisering mot Alpaca Paper REST API (Fasit for konto, cash, posisjoner).
  2. Hybrid Markedsdata-stakk:
     - Hvis MASSIVE_API_KEY (eller POLYGON_API_KEY) er satt: Henter 100% nasjonal SIP-kurs og
       komplette OPRA-opsjonskjeder med ferdigberegnede Greker (Delta, Bid/Ask) via Massive.com.
     - Ellers: Automatisk fallback til Alpaca Market Data API (IEX).
  3. Sanntids Latency Benchmark (/api/latency) mot Alpaca, Massive.com og lokal DuckDB.
  4. WebSocket-strøm (/ws/live) for lynrask live-oppdatering til nettleser-dashbordet.
  5. Human-in-the-Loop (HITL) godkjenningskø for 15-min ORB og VWAP ($5 000 maks tak per handel).
  6. Automatisk journalføring i DuckDB (%LOCALAPPDATA%/ai_trader_runtime på Windows for å unngå Google Drive-lås).
"""

import os
import json
import time
import asyncio
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, List, Any

import requests
import duckdb
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel
import uvicorn

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# Konfigurer logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("AlpacaPaperRunner")

BASE_DIR = Path(__file__).resolve().parent

# Miljøvariabler / API-nøkler (strengt kun Alpaca Paper)
ALPACA_API_KEY = (os.getenv("ALPACA_API_KEY") or os.getenv("ALPACA_KEY") or os.getenv("APCA_API_KEY_ID") or "").strip()
ALPACA_SECRET_KEY = (os.getenv("ALPACA_SECRET_KEY") or os.getenv("ALPACA_SECRET") or os.getenv("APCA_API_SECRET_KEY") or "").strip()
_raw_base = (os.getenv("ALPACA_BASE_URL") or os.getenv("ALPACA_ENDPOINT") or "https://paper-api.alpaca.markets").strip().rstrip("/")
if "paper-api.alpaca.markets" not in _raw_base:
    _raw_base = "https://paper-api.alpaca.markets"
ALPACA_BASE_URL = _raw_base if _raw_base.endswith("/v2") else f"{_raw_base}/v2"
ALPACA_DATA_URL = "https://data.alpaca.markets/v2"

MASSIVE_API_KEY = (os.getenv("MASSIVE_API_KEY") or os.getenv("POLYGON_API_KEY") or "").strip()
MASSIVE_BASE_URL = "https://api.polygon.io"  # Massive.com REST API gateway


def _runtime_db_dir() -> Path:
    """Bruker %LOCALAPPDATA%/ai_trader_runtime på Windows for å unngå Google Drive-fillåsing."""
    local_app = os.getenv("LOCALAPPDATA")
    if os.name == "nt" and local_app:
        p = Path(local_app) / "ai_trader_runtime"
        p.mkdir(parents=True, exist_ok=True)
        return p
    p = BASE_DIR / "data"
    p.mkdir(parents=True, exist_ok=True)
    return p


RUNTIME_DIR = _runtime_db_dir()
DB_PATH = str(RUNTIME_DIR / "alpaca_trading_vault.duckdb")
TAX_DB_PATH = str(RUNTIME_DIR / "tax_tracking.duckdb")


# -------------------------------------------------------------------------
# DUCKDB INNLEGG & STRUKTUR
# -------------------------------------------------------------------------
def init_databases():
    """Initialiserer DuckDB-tabeller for ordrer, strategier og skatt."""
    with duckdb.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS strategy_evaluations (
                evaluation_id VARCHAR PRIMARY KEY,
                timestamp TIMESTAMP,
                ticker VARCHAR,
                strategy_name VARCHAR,
                direction VARCHAR,
                instrument_type VARCHAR,
                underlying_price DOUBLE,
                target_delta DOUBLE,
                option_strike DOUBLE,
                contracts_or_shares INTEGER,
                entry_limit DOUBLE,
                stop_loss DOUBLE,
                take_profit DOUBLE,
                est_friction_usd DOUBLE,
                est_net_profit_usd DOUBLE,
                est_tax_nok DOUBLE,
                risk_reward_ratio DOUBLE,
                status VARCHAR,
                data_provider VARCHAR,
                ai_rationale VARCHAR
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS executed_orders (
                order_id VARCHAR PRIMARY KEY,
                client_order_id VARCHAR,
                timestamp TIMESTAMP,
                ticker VARCHAR,
                side VARCHAR,
                qty INTEGER,
                order_type VARCHAR,
                limit_price DOUBLE,
                status VARCHAR,
                broker VARCHAR,
                alpaca_order_id VARCHAR
            )
        """)

    with duckdb.connect(TAX_DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS closed_trades_tax_ledger (
                trade_id VARCHAR PRIMARY KEY,
                broker VARCHAR,
                ticker VARCHAR,
                asset_class VARCHAR,
                currency VARCHAR,
                open_date TIMESTAMP,
                close_date TIMESTAMP,
                cost_basis_ccy DOUBLE,
                proceeds_gross_ccy DOUBLE,
                fees_commissions_ccy DOUBLE,
                net_pnl_ccy DOUBLE,
                fx_open DOUBLE,
                fx_close DOUBLE,
                cost_basis_nok DOUBLE,
                proceeds_net_nok DOUBLE,
                realized_pnl_nok DOUBLE,
                tax_rate DOUBLE,
                tax_due_nok DOUBLE
            )
        """)
    logger.info("✅ DuckDB handels- og skattevelv initialisert (%s).", RUNTIME_DIR)


try:
    init_databases()
except Exception as e:
    logger.error("Feil ved initialisering av databaser: %s", e)


# -------------------------------------------------------------------------
# MARKEDSDATA-PROVIDER (HYBRID: MASSIVE.COM VS ALPACA)
# -------------------------------------------------------------------------
class MarketDataProvider:
    """
    Håndterer markedsdata fra Massive.com (dersom nøkkel finnes)
    eller faller sømløst tilbake på Alpaca Data API.
    """

    def __init__(self, massive_key: str, alpaca_key: str, alpaca_secret: str):
        self.massive_key = massive_key
        self.alpaca_headers = {
            "APCA-API-KEY-ID": alpaca_key,
            "APCA-API-SECRET-KEY": alpaca_secret,
        }
        self.active_provider = "Massive.com (SIP 100%)" if massive_key else "Alpaca Data (IEX)"
        logger.info("📡 Aktiv markedsdata-leverandør: %s", self.active_provider)

    def get_snapshot(self, ticker: str) -> Dict[str, Any]:
        """Henter siste kurs, volum og VWAP for en US-aksje."""
        ticker = ticker.upper().strip()
        if self.massive_key:
            try:
                url = f"{MASSIVE_BASE_URL}/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}?apiKey={self.massive_key}"
                r = requests.get(url, timeout=4)
                if r.status_code == 200:
                    data = r.json().get("ticker", {})
                    prev_day = data.get("prevDay", {})
                    day = data.get("day", {})
                    min_bar = data.get("min", {})
                    return {
                        "provider": "Massive.com",
                        "price": min_bar.get("c") or day.get("c") or prev_day.get("c", 145.0),
                        "vwap": day.get("vw", 144.5),
                        "volume": day.get("v", 1000000),
                        "prev_close": prev_day.get("c", 143.0),
                        "high": day.get("h", 146.0),
                        "low": day.get("l", 143.5),
                    }
            except Exception as e:
                logger.warning("Massive.com feilet for %s: %s. Bruker fallback.", ticker, e)

        if ALPACA_API_KEY and ALPACA_SECRET_KEY:
            try:
                url = f"{ALPACA_DATA_URL}/stocks/{ticker}/snapshot"
                r = requests.get(url, headers=self.alpaca_headers, timeout=4)
                if r.status_code == 200:
                    data = r.json()
                    latest_trade = data.get("latestTrade", {})
                    daily_bar = data.get("dailyBar", {})
                    prev_bar = data.get("prevDailyBar", {})
                    return {
                        "provider": "Alpaca Data",
                        "price": latest_trade.get("p", 145.0),
                        "vwap": daily_bar.get("vw", 144.5),
                        "volume": daily_bar.get("v", 800000),
                        "prev_close": prev_bar.get("c", 143.0),
                        "high": daily_bar.get("h", 146.0),
                        "low": daily_bar.get("l", 143.5),
                    }
            except Exception as e:
                logger.warning("Alpaca Data feilet for %s: %s.", ticker, e)

        mock_prices = {"NVDA": 145.20, "TSLA": 211.80, "SPY": 591.10, "AAPL": 226.40}
        p = mock_prices.get(ticker, 150.0)
        return {
            "provider": "Offline Snapshot",
            "price": p,
            "vwap": p * 0.998,
            "volume": 1500000,
            "prev_close": p * 0.992,
            "high": p * 1.012,
            "low": p * 0.988,
        }

    def get_option_contract(self, ticker: str, underlying_price: float, side: str, target_delta: float) -> Dict[str, Any]:
        """
        Finner optimal opsjonskontrakt (Slightly ITM, Delta ~0.70 eller -0.70).
        Bruker Massive.com OPRA Greker hvis tilgjengelig.
        """
        ticker = ticker.upper().strip()
        if self.massive_key:
            try:
                contract_type = "call" if "CALL" in side.upper() else "put"
                url = (
                    f"{MASSIVE_BASE_URL}/v3/snapshot/options/{ticker}"
                    f"?contract_type={contract_type}&order=asc&limit=10&apiKey={self.massive_key}"
                )
                r = requests.get(url, timeout=4)
                if r.status_code == 200:
                    results = r.json().get("results", [])
                    best = None
                    best_diff = 999.0
                    for c in results:
                        greeks = c.get("greeks", {})
                        d = greeks.get("delta")
                        if d is not None:
                            diff = abs(abs(d) - abs(target_delta))
                            if diff < best_diff:
                                best_diff = diff
                                best = c
                    if best:
                        details = best.get("details", {})
                        q = best.get("last_quote", {})
                        bid = float(q.get("bid") or 4.10)
                        ask = float(q.get("ask") or 4.25)
                        return {
                            "provider": "Massive.com OPRA",
                            "symbol": details.get("ticker", f"{ticker}_OPT"),
                            "strike": details.get("strike_price", underlying_price),
                            "delta": best.get("greeks", {}).get("delta", target_delta),
                            "bid": bid,
                            "ask": ask,
                            "mid": round((bid + ask) / 2, 2),
                        }
            except Exception as e:
                logger.warning("Massive Options snapshot feilet: %s. Bruker syntetisk ITM.", e)

        if "CALL" in side.upper():
            strike = round((underlying_price * 0.99) / 2.5) * 2.5
            intrinsic = max(0.0, underlying_price - strike)
            extrinsic = underlying_price * 0.015
            premium = round(intrinsic + extrinsic, 2)
            delta = 0.68
        else:
            strike = round((underlying_price * 1.01) / 2.5) * 2.5
            intrinsic = max(0.0, strike - underlying_price)
            extrinsic = underlying_price * 0.015
            premium = round(intrinsic + extrinsic, 2)
            delta = -0.66

        return {
            "provider": "Alpaca/Syntetisk",
            "symbol": f"{ticker}{datetime.now().strftime('%y%m%d')}{'C' if 'CALL' in side.upper() else 'P'}{int(strike * 1000):08d}",
            "strike": strike,
            "delta": delta,
            "bid": round(max(0.05, premium - 0.05), 2),
            "ask": round(premium + 0.05, 2),
            "mid": premium,
        }

    def measure_latencies(self) -> Dict[str, Any]:
        """Måler tur-retur responstid (latency i ms) mot Alpaca, Massive.com og lokal DuckDB."""
        results: Dict[str, Any] = {}

        # 1. Alpaca REST Latency
        t0 = time.perf_counter()
        alpaca_ok = False
        alpaca_status_msg = "Offline/Mock"
        try:
            r = requests.get(f"{ALPACA_BASE_URL}/clock", headers=self.alpaca_headers, timeout=2.5)
            alpaca_lat = round((time.perf_counter() - t0) * 1000, 1)
            alpaca_ok = r.status_code == 200
            alpaca_status_msg = f"HTTP {r.status_code}" if alpaca_ok else f"Status {r.status_code}"
        except Exception:
            alpaca_lat = round((time.perf_counter() - t0) * 1000, 1)

        results["alpaca"] = {
            "endpoint": "paper-api.alpaca.markets",
            "latency_ms": alpaca_lat,
            "connected": alpaca_ok,
            "status": alpaca_status_msg,
        }

        # 2. Massive.com Latency
        t0 = time.perf_counter()
        massive_ok = False
        massive_status_msg = "Offline/Fallback"
        try:
            url = f"{MASSIVE_BASE_URL}/v1/marketstatus/now"
            if self.massive_key:
                url += f"?apiKey={self.massive_key}"
            r = requests.get(url, timeout=2.5)
            massive_lat = round((time.perf_counter() - t0) * 1000, 1)
            massive_ok = r.status_code in (200, 401)
            massive_status_msg = "Active (SIP 100%)" if (self.massive_key and r.status_code == 200) else "Public/No-Key"
        except Exception:
            massive_lat = round((time.perf_counter() - t0) * 1000, 1)

        results["massive"] = {
            "endpoint": "api.polygon.io (Massive)",
            "latency_ms": massive_lat,
            "connected": massive_ok,
            "status": massive_status_msg,
            "active": bool(self.massive_key),
        }

        # 3. DuckDB Latency
        t0 = time.perf_counter()
        try:
            with duckdb.connect(DB_PATH) as conn:
                conn.execute("SELECT 1").fetchone()
            duck_lat = round((time.perf_counter() - t0) * 1000, 2)
            duck_ok = True
        except Exception:
            duck_lat = 0.5
            duck_ok = False

        results["duckdb"] = {
            "endpoint": DB_PATH,
            "latency_ms": duck_lat,
            "connected": duck_ok,
        }
        results["active_market_provider"] = self.active_provider
        results["timestamp"] = datetime.now(timezone.utc).isoformat()
        return results


market_data = MarketDataProvider(MASSIVE_API_KEY, ALPACA_API_KEY, ALPACA_SECRET_KEY)


# -------------------------------------------------------------------------
# ALPACA PAPER CLIENT (FASIT / KILDE)
# -------------------------------------------------------------------------
class AlpacaPaperClient:
    def __init__(self):
        self.headers = {
            "APCA-API-KEY-ID": ALPACA_API_KEY,
            "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
            "Content-Type": "application/json",
        }

    def get_account(self) -> Dict[str, Any]:
        """Henter live konto, cash og kjøpekraft fra Alpaca Paper."""
        if ALPACA_API_KEY and ALPACA_SECRET_KEY:
            try:
                r = requests.get(f"{ALPACA_BASE_URL}/account", headers=self.headers, timeout=4)
                if r.status_code == 200:
                    data = r.json()
                    return {
                        "connected": True,
                        "account_number": data.get("account_number", "PAPER-LIVE"),
                        "equity": float(data.get("equity", 100000.0)),
                        "cash": float(data.get("cash", 25000.0)),
                        "buying_power": float(data.get("buying_power", 50000.0)),
                        "currency": data.get("currency", "USD"),
                        "status": data.get("status", "ACTIVE"),
                    }
            except Exception as e:
                logger.warning("Kunne ikke kontakte live Alpaca API: %s. Returnerer snapshot.", e)

        return {
            "connected": False,
            "account_number": "PAPER-OFFLINE",
            "equity": 100000.00,
            "cash": 100000.00,
            "buying_power": 200000.00,
            "currency": "USD",
            "status": "MOCK_ACTIVE",
        }

    def get_positions(self) -> List[Dict[str, Any]]:
        if not (ALPACA_API_KEY and ALPACA_SECRET_KEY):
            return []
        try:
            r = requests.get(f"{ALPACA_BASE_URL}/positions", headers=self.headers, timeout=4)
            if r.status_code == 200:
                return r.json()
        except Exception as e:
            logger.warning("Feil ved henting av posisjoner: %s", e)
        return []

    def get_orders(self, status: str = "all") -> List[Dict[str, Any]]:
        if not (ALPACA_API_KEY and ALPACA_SECRET_KEY):
            return []
        try:
            r = requests.get(f"{ALPACA_BASE_URL}/orders?status={status}&limit=50", headers=self.headers, timeout=4)
            if r.status_code == 200:
                return r.json()
        except Exception as e:
            logger.warning("Feil ved henting av ordrer: %s", e)
        return []

    def submit_bracket_order(
        self, ticker: str, qty: int, side: str, limit_price: float, stop_price: float, target_price: float
    ) -> Dict[str, Any]:
        payload = {
            "symbol": ticker.upper(),
            "qty": qty,
            "side": side.lower(),
            "type": "limit",
            "time_in_force": "day",
            "limit_price": round(limit_price, 2),
            "order_class": "bracket",
            "take_profit": {"limit_price": round(target_price, 2)},
            "stop_loss": {"stop_price": round(stop_price, 2)},
        }
        try:
            r = requests.post(f"{ALPACA_BASE_URL}/orders", headers=self.headers, json=payload, timeout=5)
            if r.status_code in (200, 201):
                logger.info("🚀 Ordre eksekvert hos Alpaca Paper: %s %d stk", ticker, qty)
                return {"success": True, "data": r.json()}
            return {"success": False, "error": r.text}
        except Exception as e:
            return {"success": False, "error": str(e)}


alpaca_client = AlpacaPaperClient()


# -------------------------------------------------------------------------
# HITL GODKJENNINGSKØ & STRATEGIEVALUERING
# -------------------------------------------------------------------------
class OrderApprovalRequest(BaseModel):
    evaluation_id: str
    ticker: str
    direction: str
    qty: int
    entry_price: float
    stop_loss: float
    take_profit: float


approval_queue: List[Dict[str, Any]] = [
    {
        "evaluation_id": "OPT-NVDA-15M-CALL",
        "ticker": "NVDA",
        "strategy_name": "15-min ORB Breakout",
        "direction": "BUY_CALL",
        "instrument_type": "OPTION",
        "underlying_price": 145.20,
        "option_strike": 144.0,
        "target_delta": 0.68,
        "qty": 11,
        "entry_limit": 4.20,
        "stop_loss": 3.12,
        "take_profit": 7.08,
        "total_cost_usd": 4620.00,
        "est_friction_usd": 111.21,
        "est_net_profit_usd": 1900.10,
        "est_tax_nok": 7190.00,
        "risk_reward_ratio": 2.35,
        "expires_in_seconds": 180,
        "data_provider": market_data.active_provider,
        "ai_rationale": "NVDA brøt over 15-min High ($145.00) med 1.8x volum. Hurdle godkjent etter skatt.",
    },
    {
        "evaluation_id": "OPT-TSLA-VWAP-PUT",
        "ticker": "TSLA",
        "strategy_name": "VWAP Bearish Rejection",
        "direction": "BUY_PUT",
        "instrument_type": "OPTION",
        "underlying_price": 211.80,
        "option_strike": 212.5,
        "target_delta": -0.66,
        "qty": 8,
        "entry_limit": 5.80,
        "stop_loss": 4.05,
        "take_profit": 10.70,
        "total_cost_usd": 4640.00,
        "est_friction_usd": 80.80,
        "est_net_profit_usd": 2386.40,
        "est_tax_nok": 9030.00,
        "risk_reward_ratio": 2.59,
        "expires_in_seconds": 240,
        "data_provider": market_data.active_provider,
        "ai_rationale": "TSLA feilet motstand ved VWAP ($212.20). Nedsiderisiko fanges med ITM Put.",
    },
]


# -------------------------------------------------------------------------
# FASTAPI APP & WEBSOCKET ENGINE
# -------------------------------------------------------------------------
app = FastAPI(title="Alpaca Paper & Massive.com Cockpit Runner", version="2.5")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        logger.info("Klient tilkoblet WebSocket (Totalt: %d)", len(self.active_connections))

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
            logger.info("Klient koblet fra WebSocket")

    async def broadcast(self, message: dict):
        dead = []
        for connection in self.active_connections:
            try:
                await connection.send_text(json.dumps(message))
            except Exception:
                dead.append(connection)
        for d in dead:
            self.disconnect(d)


manager = ConnectionManager()


# -------------------------------------------------------------------------
# REST ENDEPUNKTER
# -------------------------------------------------------------------------
@app.get("/api/kb/status")
async def kb_status():
    """Returnerer status for DuckDB Finance Knowledge Base."""
    from knowledge_base_indexer import get_kb_status
    return get_kb_status()


@app.get("/api/kb/search")
async def kb_search(q: str = "", top_k: int = 5):
    """Søker i indekserte PDF-er og dokumenter (Skatte-ABC, US-NO TAX, strategier)."""
    from knowledge_base_indexer import search_knowledge_base
    return {"query": q, "results": search_knowledge_base(q, top_k=top_k)}


@app.post("/api/kb/scan")
async def kb_scan():
    """Utløser inkrementell indeksering av Finance Knowledge Base."""
    from knowledge_base_indexer import scan_knowledge_base, get_kb_status
    updated = scan_knowledge_base()
    return {"updated_files": updated, **get_kb_status()}


@app.get("/api/latency")
async def get_latency_benchmark():
    """Måler sanntids latency (ping i ms) mot Alpaca, Massive.com og lokal DuckDB."""
    return market_data.measure_latencies()


@app.get("/api/health")
async def health_check():
    return {
        "status": "healthy",
        "time": datetime.now(timezone.utc).isoformat(),
        "alpaca_broker": "paper-api.alpaca.markets",
        "market_data_provider": market_data.active_provider,
        "massive_key_configured": bool(MASSIVE_API_KEY),
        "risk_cap_per_trade_usd": 5000.0,
        "tax_rate_effective": 0.3784,
    }


@app.get("/api/status")
async def get_status():
    acct = alpaca_client.get_account()
    return {
        "connected": acct.get("connected", False),
        "broker": "Alpaca Paper v2",
        "equity": acct.get("equity", 100000.0),
        "cash": acct.get("cash", 100000.0),
        "buying_power": acct.get("buying_power", 0.0),
        "data_provider": market_data.active_provider,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/positions")
async def get_positions_alias():
    return alpaca_client.get_positions()


@app.get("/api/alpaca/account")
async def get_alpaca_account():
    return alpaca_client.get_account()


@app.get("/api/alpaca/positions")
async def get_alpaca_positions():
    return alpaca_client.get_positions()


@app.get("/api/alpaca/orders")
async def get_alpaca_orders():
    return alpaca_client.get_orders("all")


@app.get("/api/account/live")
async def get_live_account():
    """Returnerer konto, posisjoner og dagens ordrer fra Alpaca."""
    account = alpaca_client.get_account()
    positions = alpaca_client.get_positions()
    orders = alpaca_client.get_orders("all")
    return {
        "account": account,
        "positions": positions,
        "orders_count": len(orders),
        "orders": orders[:10],
        "data_provider": market_data.active_provider,
    }


@app.get("/api/hitl/queue")
async def get_approval_queue():
    return {"queue": approval_queue, "count": len(approval_queue)}


@app.post("/api/hitl/approve")
async def approve_order(req: OrderApprovalRequest):
    """Godkjenner et forslag og ruter det til Alpaca som OCO Bracket."""
    global approval_queue
    item = next((x for x in approval_queue if x["evaluation_id"] == req.evaluation_id), None)
    if not item:
        raise HTTPException(status_code=404, detail="Signal ikke funnet i godkjenningskø")

    total_cost = req.qty * req.entry_price * (100 if "OPTION" in item.get("instrument_type", "") else 1)
    if total_cost > 5000.0:
        raise HTTPException(status_code=400, detail=f"Allokering (${total_cost:.2f}) overskrider taket på $5 000.")

    side = "buy" if "BUY" in req.direction.upper() else "sell"
    result = alpaca_client.submit_bracket_order(
        ticker=req.ticker,
        qty=req.qty,
        side=side,
        limit_price=req.entry_price,
        stop_price=req.stop_loss,
        target_price=req.take_profit,
    )

    try:
        with duckdb.connect(DB_PATH) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO executed_orders
                VALUES (?, ?, CURRENT_TIMESTAMP, ?, ?, ?, 'LIMIT_BRACKET', ?, 'SUBMITTED', 'Alpaca', ?)
                """,
                [
                    f"ORD-{req.evaluation_id}",
                    req.evaluation_id,
                    req.ticker,
                    side,
                    req.qty,
                    req.entry_price,
                    result.get("data", {}).get("id", "MOCK_AL_ID"),
                ],
            )
    except Exception as e:
        logger.error("Feil ved lagring i DuckDB: %s", e)

    approval_queue = [x for x in approval_queue if x["evaluation_id"] != req.evaluation_id]

    await manager.broadcast({
        "type": "ORDER_APPROVED",
        "evaluation_id": req.evaluation_id,
        "ticker": req.ticker,
        "alpaca_result": result,
    })

    return {"status": "APPROVED_AND_SUBMITTED", "alpaca_response": result}


@app.post("/api/hitl/reject/{evaluation_id}")
async def reject_order(evaluation_id: str):
    """Forkaster en ordre fra godkjenningskøen."""
    global approval_queue
    approval_queue = [x for x in approval_queue if x["evaluation_id"] != evaluation_id]
    await manager.broadcast({"type": "ORDER_REJECTED", "evaluation_id": evaluation_id})
    return {"status": "REJECTED", "evaluation_id": evaluation_id}


@app.get("/api/tax/summary")
async def get_tax_summary():
    """Henter akkumulert norsk skattekrav og historikk fra DuckDB."""
    try:
        from tax_tracker_duckdb import NorwegianTaxTracker
        tracker = NorwegianTaxTracker()
        try:
            return tracker.get_annual_tax_summary(tax_year=datetime.now().year)
        finally:
            tracker.close()
    except Exception as e:
        return {"error": str(e), "total_tax_due_nok": 0.0, "effective_tax_rate": 0.3784, "currency": "NOK"}


@app.websocket("/ws/live")
async def websocket_live_stream(websocket: WebSocket):
    """Sanntids WebSocket-strøm som oppdaterer dashbordet fortløpende."""
    await manager.connect(websocket)
    try:
        while True:
            account = alpaca_client.get_account()
            positions = alpaca_client.get_positions()
            latency_info = market_data.measure_latencies()
            payload = {
                "type": "TICKER_UPDATE",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "account": account,
                "positions_count": len(positions),
                "approval_queue_count": len(approval_queue),
                "approval_queue": approval_queue,
                "data_provider": market_data.active_provider,
                "latency": latency_info,
            }
            await websocket.send_text(json.dumps(payload))
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        logger.warning("WebSocket feil: %s", e)
        manager.disconnect(websocket)


@app.get("/")
async def serve_index():
    idx = BASE_DIR / "index.html"
    if idx.exists():
        return FileResponse(str(idx))
    return JSONResponse({"status": "Alpaca Paper Runner aktiv", "docs": "/docs"})


@app.get("/v2-pro")
async def serve_v2_pro():
    pro = BASE_DIR / "ui" / "dashboards" / "AI_Trading_Terminal_v2_-_Pro_Dashboard.html"
    if pro.exists():
        return FileResponse(str(pro))
    raise HTTPException(status_code=404, detail="Pro Dashboard ikke funnet")


if __name__ == "__main__":
    port = int(os.getenv("ALPACA_RUNNER_PORT", "8001"))
    logger.info("=" * 75)
    logger.info("STARTER ALPACA PAPER LIVE RUNNER & HYBRID MARKET DATA SERVER (PORT %d)", port)
    logger.info("Markedsdata-leverandør    : %s", market_data.active_provider)
    logger.info("Maks risikotak per handel : $5 000.00 USD")
    logger.info("Norsk skatteregnskap      : 37.84 %% i %s", TAX_DB_PATH)
    logger.info("=" * 75)
    uvicorn.run("alpaca_paper_runner:app", host="0.0.0.0", port=port, reload=False)
