"""
Dynamic Portfolio & Bracket Order Recommender (`dynamic_recommender.py`)
========================================================================
Kalkulerer og forbereder en komplett USD-portefølje for Alpaca Paper Trading.
Tilpasser posisjonsstørrelser og ATR(14)-baserte Stop-Loss / Take-Profit nivåer.
"""

from __future__ import annotations

import argparse
import os
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from alpaca_execution import AlpacaClient


PORTFOLIO_CONFIGS: Dict[int, Dict[str, Any]] = {
    1: {
        "navn": "Konservativ Profil (Index Core & Low-Vol)",
        "beskrivelse": "Fokus på kapitalbevaring, brede ETF-er og 20% kontantbuffer.",
        "cash_pct": 0.20,
        "circuit_breaker": 0.04,
        "atr_mult": 1.2,
        "rr_ratio": 2.0,
        "reward_risk_ratio": 2.0,
        "universe": [
            {"symbol": "SPY", "price": 590.0, "atr": 4.5, "weight": 0.25},
            {"symbol": "QQQ", "price": 510.0, "atr": 5.8, "weight": 0.25},
            {"symbol": "XLV", "price": 145.0, "atr": 1.4, "weight": 0.25},
            {"symbol": "IWM", "price": 230.0, "atr": 3.4, "weight": 0.25},
        ],
    },
    2: {
        "navn": "Balansert Profil (Multi-Sektor Momentum)",
        "beskrivelse": "Klassisk systematisk momentum på tvers av ledende S&P 500-sektorer.",
        "cash_pct": 0.10,
        "circuit_breaker": 0.06,
        "atr_mult": 1.8,
        "rr_ratio": 2.2,
        "reward_risk_ratio": 2.2,
        "universe": [
            {"symbol": "AAPL", "price": 225.0, "atr": 3.8, "weight": 0.20},
            {"symbol": "MSFT", "price": 425.0, "atr": 6.2, "weight": 0.20},
            {"symbol": "JPM", "price": 240.0, "atr": 3.6, "weight": 0.20},
            {"symbol": "CAT", "price": 400.0, "atr": 6.5, "weight": 0.20},
            {"symbol": "XOM", "price": 120.0, "atr": 2.1, "weight": 0.20},
        ],
    },
    3: {
        "navn": "Aggressiv Profil (Høy-Beta AI & Tech Outliers)",
        "beskrivelse": "Maksimal kapitalvekst med vide ATR-stopp for å unngå whipsaws.",
        "cash_pct": 0.05,
        "circuit_breaker": 0.08,
        "atr_mult": 2.5,
        "rr_ratio": 2.5,
        "reward_risk_ratio": 2.5,
        "universe": [
            {"symbol": "NVDA", "price": 145.0, "atr": 5.2, "weight": 0.17},
            {"symbol": "AVGO", "price": 165.0, "atr": 5.8, "weight": 0.17},
            {"symbol": "TSLA", "price": 340.0, "atr": 16.5, "weight": 0.16},
            {"symbol": "META", "price": 565.0, "atr": 14.2, "weight": 0.17},
            {"symbol": "PLTR", "price": 65.0, "atr": 2.8, "weight": 0.17},
            {"symbol": "AMZN", "price": 205.0, "atr": 4.2, "weight": 0.16},
        ],
    },
}

PROFILE_CONFIGS: Dict[str, Dict[str, Any]] = {str(k): v for k, v in PORTFOLIO_CONFIGS.items()}


def _fetch_live_prices(symbols: List[str]) -> Dict[str, float]:
    """Fetch latest live IEX trade prices from Alpaca Market Data API (skipped under pytest)."""
    if os.environ.get("PYTEST_CURRENT_TEST") or not symbols:
        return {}
    try:
        import requests
        from dotenv import load_dotenv

        load_dotenv(override=True)
        client = AlpacaClient()
        resp = requests.get(
            "https://data.alpaca.markets/v2/stocks/trades/latest",
            params={"symbols": ",".join(symbols), "feed": "iex"},
            headers=client.headers,
            timeout=6,
        )
        if resp.status_code != 200:
            return {}
        trades = resp.json().get("trades", {})
        return {sym: float(info["p"]) for sym, info in trades.items() if info and info.get("p")}
    except Exception:
        return {}


def calculate_portfolio(
    profile_id: int,
    total_capital: float = 100000.0,
    use_live_prices: bool = True,
) -> Dict[str, Any]:
    cfg = PORTFOLIO_CONFIGS.get(int(profile_id), PORTFOLIO_CONFIGS[2])
    investable_capital = float(total_capital) * (1.0 - cfg["cash_pct"])

    live_prices = _fetch_live_prices([it["symbol"] for it in cfg["universe"]]) if use_live_prices else {}
    orders: List[Dict[str, Any]] = []
    total_allocated = 0.0

    for item in cfg["universe"]:
        ref_price = float(item["price"])
        live_p = live_prices.get(item["symbol"])
        # Use +0.1% marketable limit on live price so orders fill promptly during open US market
        price = round(live_p * 1.001, 2) if live_p and live_p > 0 else ref_price
        atr = round(float(item["atr"]) * (price / ref_price), 2) if ref_price > 0 else float(item["atr"])

        target_val = investable_capital * item["weight"]
        qty = int(target_val // price)
        if qty == 0:
            continue

        pos_val = qty * price
        total_allocated += pos_val

        sl_dist = atr * cfg["atr_mult"]
        tp_dist = sl_dist * cfg["rr_ratio"]

        sl_price = round(price - sl_dist, 2)
        tp_price = round(price + tp_dist, 2)

        orders.append(
            {
                "symbol": item["symbol"],
                "ticker": item["symbol"],
                "qty": qty,
                "price": price,
                "price_source": "live_alpaca" if live_p else "reference",
                "atr": atr,
                "pos_val": round(pos_val, 2),
                "pos_value": round(pos_val, 2),
                "weight": round((pos_val / total_capital) * 100.0, 2),
                "weight_pct": round((pos_val / total_capital) * 100.0, 2),
                "stop_loss": sl_price,
                "sl_price": sl_price,
                "take_profit": tp_price,
                "tp_price": tp_price,
                "sl_pct": round(((price - sl_price) / price) * 100.0, 2),
                "tp_pct": round(((tp_price - price) / price) * 100.0, 2),
                "rr": cfg["rr_ratio"],
                "rr_ratio": cfg["rr_ratio"],
            }
        )

    return {
        "config": cfg,
        "total_capital": float(total_capital),
        "allocated": round(total_allocated, 2),
        "cash_reserve": round(float(total_capital) - total_allocated, 2),
        "orders": orders,
    }


class DynamicRecommender:
    """Institutional Alpaca Paper portfolio recommender with Paper-Only endpoint safeguard."""

    def __init__(self, endpoint: Optional[str] = None) -> None:
        raw_ep = (
            endpoint
            or os.getenv("ALPACA_ENDPOINT")
            or os.getenv("alpaca_endpoint")
            or "https://paper-api.alpaca.markets/v2"
        ).rstrip("/")
        if not raw_ep.endswith("/v2"):
            raw_ep = f"{raw_ep}/v2"
        parsed = urlparse(raw_ep)
        if parsed.scheme != "https" or (parsed.hostname or "").lower() != "paper-api.alpaca.markets":
            raise ValueError(
                f"SIKKERHETSSPERRE: Kun Alpaca Paper Trading er tillatt. Avvist endepunkt: {raw_ep}"
            )
        self.endpoint = raw_ep

    def fetch_live_equity(self, fallback_capital: float = 100_000.0) -> float:
        try:
            acct = AlpacaClient(endpoint=self.endpoint).get_account()
            eq = float(acct.get("equity", fallback_capital))
            return eq if eq > 0 else fallback_capital
        except Exception:
            return float(fallback_capital)

    def generate_recommendation(
        self,
        profile_key: str = "2",
        custom_capital: Optional[float] = None,
    ) -> Dict[str, Any]:
        p_id = int(str(profile_key).strip()) if str(profile_key).strip() in ("1", "2", "3") else 2
        total_eq = float(custom_capital) if custom_capital is not None else self.fetch_live_equity()
        calc = calculate_portfolio(p_id, total_capital=total_eq)
        cfg = calc["config"]
        return {
            "profile_key": str(p_id),
            "profile_name": cfg["navn"],
            "description": cfg["beskrivelse"],
            "total_equity": total_eq,
            "allocated_total": calc["allocated"],
            "remaining_cash": calc["cash_reserve"],
            "cash_buffer_pct": round(cfg["cash_pct"] * 100.0, 2),
            "circuit_breaker_pct": round(cfg["circuit_breaker"] * 100.0, 2),
            "atr_mult": cfg["atr_mult"],
            "rr_ratio": cfg["rr_ratio"],
            "proposals": calc["orders"],
        }


def print_portfolio(rec: Dict[str, Any]) -> None:
    cfg = rec["config"]
    print("=" * 86)
    print(f"PORTEFØLJEANBEFALING: {cfg['navn'].upper()}")
    print("=" * 86)
    print(f"Beskrivelse      : {cfg['beskrivelse']}")
    print(f"Total Egenkapital: ${rec['total_capital']:,.2f} USD")
    print(f"Allokert Aksjer  : ${rec['allocated']:,.2f} USD ({(rec['allocated']/rec['total_capital'])*100:.1f} %)")
    print(f"Kontantreserve   : ${rec['cash_reserve']:,.2f} USD ({(rec['cash_reserve']/rec['total_capital'])*100:.2f} %)")
    print(f"Circuit Breaker  : Maksimalt tillatt porteføljetap = {cfg['circuit_breaker']*100:.1f} %")
    print("-" * 86)
    print(f"{'Ticker':<7} {'Antall':<7} {'Kurs ($)':<10} {'Posisjon ($)':<14} {'Vekt':<8} {'Stop-Loss':<14} {'Take-Profit':<14} {'R/R':<6}")
    print("-" * 86)
    for o in rec["orders"]:
        print(
            f"{o['symbol']:<7} {o['qty']:<7} ${o['price']:<9.2f} ${o['pos_val']:<13,.2f} {o['weight']:>5.1f} % "
            f"${o['stop_loss']:.2f} (-{o['sl_pct']:.2f}%) ${o['take_profit']:.2f} (+{o['tp_pct']:.2f}%) {o['rr']:.1f}:1"
        )
    print("=" * 86)


def main() -> None:
    parser = argparse.ArgumentParser(description="Dynamisk Portefølje Recommender for Alpaca Paper")
    parser.add_argument("--profile", type=int, default=2, choices=[1, 2, 3], help="1=Konservativ, 2=Balansert, 3=Aggressiv")
    parser.add_argument("--capital", type=float, default=100000.0, help="Total startkapital i USD")
    parser.add_argument("--execute", action="store_true", help="Send ordrene direkte til Alpaca Paper Trading")
    args = parser.parse_args()

    rec = calculate_portfolio(args.profile, args.capital)
    print_portfolio(rec)

    if args.execute:
        client = AlpacaClient()
        print("\nSender bracket-ordrer til Alpaca Paper...")
        for o in rec["orders"]:
            try:
                res = client.place_bracket_order(
                    symbol=o["symbol"],
                    qty=o["qty"],
                    limit_price=o["price"],
                    take_profit_price=o["take_profit"],
                    stop_loss_price=o["stop_loss"],
                )
                print(f"Kjøp {o['symbol']} {o['qty']} stk: OK (Ordre-ID: {str(res.get('id', 'OK'))[:8]})")
            except Exception as exc:
                print(f"Feil ved {o['symbol']}: {exc}")


if __name__ == "__main__":
    main()
