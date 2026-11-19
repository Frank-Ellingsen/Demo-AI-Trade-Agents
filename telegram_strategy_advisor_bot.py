"""
Telegram Strategy Advisor & HITL Bot (`telegram_strategy_advisor_bot.py`)
=========================================================================
Kombinert rådgiver og eksekveringslytter:
1. Svarer på fritekst-spørsmål om strategier, VIX, risiko, rulebook og Alpaca/OBX-allokering.
2. Sender handelssignaler med interaktive godkjenningsknapper.
3. Kaller AlpacaClient ved godkjenning eller failover_approver ved timeout/feil.
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from typing import Any, Dict, Optional

import requests
from dotenv import load_dotenv

from alpaca_execution import AlpacaClient
from dynamic_recommender import DynamicRecommender
from failover_approver import send_backup_email

load_dotenv()
logger = logging.getLogger("TelegramStrategyAdvisor")

STRATEGY_KNOWLEDGE: Dict[str, str] = {
    "regime": (
        "🧭 *AI Regime Switcher & VIX-filter*\n"
        "• *VIX < 18 (Risk-On):* Full allokering til US Liquid Momentum (S&P 500) med $0 kurtasje på Alpaca Paper.\n"
        "• *VIX 18–24 (Nøytral):* Roterer mot Systematic Covered Calls (SPY) for løpende premieinntekt.\n"
        "• *VIX > 24 (Risk-Off):* Kapitalbevaring i kontanter / T-Bills inntil volatiliteten faller.\n"
        "• *Failover:* Primær Telegram -> Sekundær E-post (`/approve`) -> Tertiær Web Terminal."
    ),
    "momentum": (
        "📊 *US Liquid Momentum (S&P 500)*\n"
        "• *Univers:* Topp 500 mest likvide aksjer i USA (volum > $50M daglig).\n"
        "• *Regime:* Krever SPY > SMA200 og VIX < 22.\n"
        "• *Historisk profil:* +68.8% avkastning med Sortino 6.62 og Max Drawdown 4.75%.\n"
        "• *Hovedrisiko:* Whipsaws i sidelengs markeder der ATR stop-loss kutter små tap (-3% til -4%).\n"
        "• *Megler:* Alpaca Paper ($0 kurtasje, USD-konto)."
    ),
    "options": (
        "🛡️ *Systematic Covered Calls (SPY)*\n"
        "• *Logikk:* Eier SPY og selger 30-dagers 2.5% OTM calls for ~1.35% månedlig premie.\n"
        "• *Profil:* Lavere svingninger (volatilitet 12.2% vs 16.2%), men kapper oppsiden i kraftige rallyer.\n"
        "• *Anbefales når:* Markedet er rolig eller svakt stigende."
    ),
    "otc": (
        "⚠️ *Hvorfor unngå OTC & Micro-Caps?*\n"
        "• *Funn:* 1.5% - 3.0% bid-ask spread spiser opp all teoretisk gevinst.\n"
        "• *Likviditet:* Ved panikk er det ingen kjøpere på bid-siden, og stop-loss feiler.\n"
        "• *Konklusjon:* Hold deg strengt til store, likvide S&P 500-aksjer."
    ),
    "obx": (
        "🇳🇴 *OBX 15 Momentum (Oslo Børs)*\n"
        "• *Univers:* De 15 mest omsatte aksjene på OSE.\n"
        "• *Spesialfilter:* Krever 2-dagers Brent Crude > -2.5% for å unngå oljekrakk.\n"
        "• *Megler:* Saxo Bank (Aksjesparekonto / ASK i NOK) som referansemodell."
    ),
    "rulebook": (
        "📜 *Trading Agent Rulebook (4 Agenter + 3-Trinns Failover)*\n"
        "• *Agent 1 (News):* Hard Veto ved kvartalsrapport innen 48t (Earnings Trap Protection).\n"
        "• *Agent 2 (Quant):* 12-1m momentum + RSI(2) + P > SMA200 + volum > 1.2x SMA20.\n"
        "• *Agent 3 (Risk):* 1.8x ATR(14) SL, maks 2 posisjoner / 35% per sektor, 6% Circuit Breaker.\n"
        "• *Agent 4 (Exec):* Kun Limit + OCO Bracket etter godkjenning (Telegram -> E-post -> Web)."
    ),
}


class StrategyAdvisor:
    """Stateless strategy & risk knowledge advisor used by Web UI, Streamlit, and Telegram."""

    @staticmethod
    def answer_query(query: str) -> str:
        q = (query or "").strip().lower()
        if "anbefal" in q or "profil" in q:
            p_key = "1" if "1" in q or "konservativ" in q else ("3" if "3" in q or "aggressiv" in q else "2")
            rec = DynamicRecommender().generate_recommendation(p_key, custom_capital=100_000.0)
            lines = [
                f"🎯 *Porteføljeanbefaling: {rec['profile_name']}*",
                f"• Kapital: ${rec['total_equity']:,.0f} USD | Allokert: ${rec['allocated_total']:,.0f} | Buffer: {rec['cash_buffer_pct']:.1f}%",
                f"• Circuit Breaker: -{rec['circuit_breaker_pct']:.1f}% | ATR-mult: {rec['atr_mult']}x | R/R: {rec['rr_ratio']}:1",
            ]
            for p in rec["proposals"]:
                lines.append(
                    f"  - `{p['ticker']}`: {p['qty']} stk @ ${p['price']:.2f} (SL ${p['sl_price']:.2f} / TP ${p['tp_price']:.2f})"
                )
            return "\n".join(lines)
        if "regime" in q or "vix" in q:
            return STRATEGY_KNOWLEDGE["regime"]
        if "rulebook" in q or "regel" in q or "failover" in q or "veto" in q:
            return STRATEGY_KNOWLEDGE["rulebook"]
        if "momentum" in q or "s&p" in q or "usa" in q:
            return STRATEGY_KNOWLEDGE["momentum"]
        if "opsjon" in q or "call" in q or "covered" in q:
            return STRATEGY_KNOWLEDGE["options"]
        if "otc" in q or "small" in q or "penny" in q:
            return STRATEGY_KNOWLEDGE["otc"]
        if "obx" in q or "norge" in q or "olje" in q:
            return STRATEGY_KNOWLEDGE["obx"]
        return (
            "🤖 *AI Strategi-Rådgiver*\n"
            "Spør meg om: `regime`, `momentum`, `opsjoner`, `otc`, `obx`, `rulebook`, eller `anbefal 1/2/3`."
        )


class TelegramBotListener:
    """Lightweight Telegram sender/listener compatible with runner_server.py and app.py."""

    def __init__(self) -> None:
        self.bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.chat_id = str(os.getenv("TELEGRAM_CHAT_ID", "")).strip()
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}"

    def send_text(self, text: str, reply_markup: Optional[dict] = None) -> Dict[str, Any]:
        if not self.bot_token or not self.chat_id:
            raise ValueError("Mangler TELEGRAM_BOT_TOKEN eller TELEGRAM_CHAT_ID i .env")
        payload: Dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "Markdown",
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        resp = requests.post(f"{self.base_url}/sendMessage", json=payload, timeout=10)
        return resp.json()


class TelegramAdvisorBot:
    """Combined Telegram Advisor + HITL Signal Sender with automatic Failover Email backup."""

    def __init__(self) -> None:
        self.bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.chat_id = str(os.getenv("TELEGRAM_CHAT_ID", "")).strip()
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}"
        self.alpaca: Optional[AlpacaClient] = None
        try:
            self.alpaca = AlpacaClient()
        except Exception:
            self.alpaca = None
        self.pending_trades: Dict[str, Dict[str, Any]] = {}

    def send_message(self, text: str, reply_markup: Optional[dict] = None) -> bool:
        if not self.bot_token or not self.chat_id:
            logger.error("Mangler TELEGRAM_BOT_TOKEN eller CHAT_ID i .env")
            return False
        payload: Dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "Markdown",
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            resp = requests.post(f"{self.base_url}/sendMessage", json=payload, timeout=10)
            return resp.status_code == 200
        except Exception as exc:
            logger.error("Feil ved sending til Telegram: %s", exc)
            return False

    def send_trade_signal(
        self,
        trade_id: str,
        symbol: str,
        qty: int,
        limit_price: float,
        sl: float,
        tp: float,
        reason: str,
    ) -> bool:
        self.pending_trades[trade_id] = {
            "symbol": symbol,
            "qty": qty,
            "limit": limit_price,
            "sl": sl,
            "tp": tp,
            "reason": reason,
            "created_at": time.time(),
        }
        sl_pct = ((limit_price - sl) / limit_price) * 100.0 if limit_price else 0.0
        tp_pct = ((tp - limit_price) / limit_price) * 100.0 if limit_price else 0.0
        text = (
            f"⚡ *NYTT HANDELSSIGNAL: {symbol}*\n"
            f"• *Volum:* {qty} aksjer (~${qty * limit_price:,.2f})\n"
            f"• *Limit Kurs:* ${limit_price:.2f}\n"
            f"• *Stop-Loss (ATR):* ${sl:.2f} (-{sl_pct:.1f}%)\n"
            f"• *Take-Profit:* ${tp:.2f} (+{tp_pct:.1f}%)\n"
            f"• *Begrunnelse:* {reason}\n\n"
            "_Trykk på en av knappene under for å ta stilling:_"
        )
        markup = {
            "inline_keyboard": [
                [
                    {"text": "✅ Godkjenn (Kjøp)", "callback_data": f"approve_{trade_id}"},
                    {"text": "❌ Avvis", "callback_data": f"reject_{trade_id}"},
                ],
                [{"text": "❓ Forklar Risiko", "callback_data": f"explain_{trade_id}"}],
            ]
        }
        success = self.send_message(text, reply_markup=markup)
        if not success:
            logger.warning("Telegram feilet. Starter failover e-post / web-godkjenning...")
            send_backup_email(trade_id, symbol, qty, limit_price, sl, tp, reason)
        return success

    def handle_advisor_query(self, query: str) -> str:
        return StrategyAdvisor.answer_query(query)


def main() -> None:
    parser = argparse.ArgumentParser(description="Telegram Strategy Advisor & HITL Bot")
    parser.add_argument("--query", type=str, default="", help="Skriv ut rådgiversvar i terminalen")
    parser.add_argument("--send-query", type=str, default="", help="Send rådgiversvar til Telegram")
    args = parser.parse_args()

    if args.query:
        print(StrategyAdvisor.answer_query(args.query))
    elif args.send_query:
        ans = StrategyAdvisor.answer_query(args.send_query)
        print(ans)
        TelegramBotListener().send_text(ans)
    else:
        print(StrategyAdvisor.answer_query("regime"))


if __name__ == "__main__":
    main()
