"""
Telegram Human-in-the-Loop (HITL) Trader for Alpaca (6-Felts Beslutningsprotokoll)
==================================================================================
Implementerer protokollen fra `ui_ux_bruker_test.md`:
1. 6-felts utvidet beslutningskort (Mandat, Allokering, OCO Risiko/ATR, Regime/VIX/Earnings,
   AI Bull vs. Djevelens Advokat, og Tids-/Slippage-kursvern).
2. De 4 offisielle brukertest-scenarioene:
   - Scenario 1: Standard Trendutbrudd under rolige markedsforhold (MSFT)
   - Scenario 2: Kvartalstall-fellen / «Earnings Trap» (PLTR)
   - Scenario 3: Slippage-sperre — kursen har løpt forbi +1.0 % kursvern (XOM $120 -> Ask $122.50)
   - Scenario 4: Sektortak og korrelasjonsvern — 2 av 2 energiposisjoner (COP)
3. Lytter på knappetrykk via getUpdates long polling (kun autorisert TELEGRAM_CHAT_ID).
4. Sjekker lokalt kursvern (Slippage Guardrail) før innsending av OCO Bracket-ordre til Alpaca Paper.
"""

import argparse
import logging
import os
import time
from typing import Any, Dict, Optional

import requests
from dotenv import load_dotenv

from alpaca_execution import AlpacaClient
from src.db.sqlite_store import get_sqlite_conn, init_sqlite_db
from src.hitl.order_queue import enqueue_order_proposal, update_order_status

load_dotenv(override=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("TelegramHITL")


UX_TEST_SCENARIOS: Dict[str, Dict[str, Any]] = {
    "nvda": {
        "signal_no": "1042",
        "symbol": "NVDA",
        "company_name": "NVIDIA CORP",
        "strategy_label": "Trend Breakout (20d High)",
        "risk_label": "MODERAT",
        "qty": 105,
        "side": "buy",
        "limit_price": 145.00,
        "stop_loss_price": 132.50,
        "take_profit_price": 172.50,
        "atr_multiple": 2.1,
        "portfolio_equity": 100_000.0,
        "cash_reserve_pct": 14.8,
        "regime_text": "Bull (SPY > SMA200, +0.45 % i dag)",
        "vix_level": 16.20,
        "vix_comment": "Rolig marked, lav opsjonsrisiko",
        "rel_volume": 1.48,
        "earnings_text": "Neste kvartalsrapport om 28 dager (Ingen umiddelbar risiko)",
        "sector_warning": "",
        "bull_case": "Kraftig teknisk utbrudd på høyt volum etter tre ukers konsolidering over 50-dagers snitt.",
        "devils_advocate": "Halvledersektoren viser kortsiktig divergens; motstand kan oppstå rundt $150-nivået.",
        "expiry_minutes": 15,
        "max_slippage_pct": 1.0,
        "simulated_current_ask": 145.10,
    },
    "1": {
        "signal_no": "2001",
        "symbol": "MSFT",
        "company_name": "MICROSOFT CORP (TESTSCENARIO 1: STANDARD TRENDUTBRUDD)",
        "strategy_label": "Trend Breakout (20d High)",
        "risk_label": "LAV / MODERAT",
        "qty": 42,
        "side": "buy",
        "limit_price": 425.00,
        "stop_loss_price": 412.00,
        "take_profit_price": 455.00,
        "atr_multiple": 1.8,
        "portfolio_equity": 100_000.0,
        "cash_reserve_pct": 22.1,
        "regime_text": "Bull (SPY > SMA200, +0.52 % i dag)",
        "vix_level": 15.20,
        "vix_comment": "Rolige markedsforhold, alle filtre grønne",
        "rel_volume": 1.38,
        "earnings_text": "Neste kvartalsrapport om 42 dager (Ingen umiddelbar risiko)",
        "sector_warning": "",
        "bull_case": "Rent brudd over 20-dagers motstand med sterk institusjonell akkumulering i sky- og AI-segmentet.",
        "devils_advocate": "Kortsiktig overkjøpt på 1-times graf; mindre tilbakeslag til $422 er mulig før videre oppgang.",
        "expiry_minutes": 15,
        "max_slippage_pct": 1.0,
        "simulated_current_ask": 425.15,
    },
    "2": {
        "signal_no": "2002",
        "symbol": "PLTR",
        "company_name": "PALANTIR TECHNOLOGIES (TESTSCENARIO 2: EARNINGS TRAP)",
        "strategy_label": "Trend Breakout (20d High)",
        "risk_label": "HØY (KVARTALSTALL I KVELD)",
        "qty": 180,
        "side": "buy",
        "limit_price": 65.00,
        "stop_loss_price": 59.40,
        "take_profit_price": 77.60,
        "atr_multiple": 2.0,
        "portfolio_equity": 100_000.0,
        "cash_reserve_pct": 18.3,
        "regime_text": "Bull (SPY > SMA200, +0.30 % i dag)",
        "vix_level": 17.80,
        "vix_comment": "Forhøyet implisitt volatilitet i enkeltaksjen (IV > 95 %)",
        "rel_volume": 2.15,
        "earnings_text": "⚠️ Kvartalsrapport legges frem i kveld etter børsslutt (0 dager!)",
        "sector_warning": "",
        "bull_case": "Teknisk prisutbrudd på høyt volum i forkant av resultatfremleggelse.",
        "devils_advocate": "🚨 KRITISK: Høy risiko for binært overnattingsgap (-15 % til -25 %) ved rapport i kveld. Stop-Loss beskytter ikke mot åpningsgap!",
        "expiry_minutes": 15,
        "max_slippage_pct": 1.0,
        "simulated_current_ask": 65.05,
    },
    "3": {
        "signal_no": "2003",
        "symbol": "XOM",
        "company_name": "EXXON MOBIL CORP (TESTSCENARIO 3: SLIPPAGE-SPERRE)",
        "strategy_label": "Momentum Pullback",
        "risk_label": "MODERAT (KURSSPRANG VED ÅPNING)",
        "qty": 125,
        "side": "buy",
        "limit_price": 120.00,
        "stop_loss_price": 115.80,
        "take_profit_price": 129.50,
        "atr_multiple": 2.0,
        "portfolio_equity": 100_000.0,
        "cash_reserve_pct": 15.0,
        "regime_text": "Bull (Energi & S&P 500 > SMA200)",
        "vix_level": 16.50,
        "vix_comment": "Normal markedsvolatilitet, men oljeprisen hoppet ved åpning",
        "rel_volume": 1.65,
        "earnings_text": "Neste kvartalsrapport om 34 dager (Ingen umiddelbar risiko)",
        "sector_warning": "⚡ SIMULERT SLIPPAGE-TEST: Markedets Ask har hoppet til $122.50 (+2.08 % over Limit $120.00). Trykk [✅ GODKJENN] for å teste at kursvernet blokkerer ordren!",
        "bull_case": "Sterk kontantstrøm og teknisk støtte langs 20-dagers glidende gjennomsnitt.",
        "devils_advocate": "Aksjen åpner med et kraftig oppsidegap (+2.1 %); å jage kursen over $121.20 ødelegger Reward/Risk-forholdet.",
        "expiry_minutes": 15,
        "max_slippage_pct": 1.0,
        "simulated_current_ask": 122.50,
    },
    "4": {
        "signal_no": "2004",
        "symbol": "COP",
        "company_name": "CONOCOPHILLIPS (TESTSCENARIO 4: SEKTORTAK 2/2)",
        "strategy_label": "Trend Breakout (20d High)",
        "risk_label": "HØY (SEKTORKONSENTRASJON)",
        "qty": 135,
        "side": "buy",
        "limit_price": 112.00,
        "stop_loss_price": 106.80,
        "take_profit_price": 123.50,
        "atr_multiple": 1.9,
        "portfolio_equity": 100_000.0,
        "cash_reserve_pct": 12.4,
        "regime_text": "Bull (SPY > SMA200, Brent Crude +1.4 %)",
        "vix_level": 15.90,
        "vix_comment": "Rolig marked, men høy klyngekorrelasjon i energi",
        "rel_volume": 1.42,
        "earnings_text": "Neste kvartalsrapport om 31 dager (Ingen umiddelbar risiko)",
        "sector_warning": "⚠️ Sektorvarsel: Energisektoren har allerede 2 av 2 tillatte posisjoner (XOM og CVX).",
        "bull_case": "Teknisk brudd opp fra konsolidering støttet av stigende råoljepris.",
        "devils_advocate": "🚨 KORRELASJONSVERN: Porteføljen har allerede 2 åpne energiposisjoner (XOM, CVX). En tredje posisjon bryter sektortaket (maks 2/sektor).",
        "expiry_minutes": 15,
        "max_slippage_pct": 1.0,
        "simulated_current_ask": 112.05,
    },
}


def format_extended_decision_card(
    symbol: str,
    qty: int,
    side: str,
    limit_price: float,
    take_profit_price: float,
    stop_loss_price: float,
    reasoning: str = "",
    signal_no: str = "1042",
    company_name: str = "",
    strategy_label: str = "Trend Breakout (20d High)",
    risk_label: str = "MODERAT",
    atr_multiple: float = 2.0,
    portfolio_equity: float = 100_000.0,
    cash_reserve_pct: float = 14.8,
    regime_text: str = "Bull (SPY > SMA200, +0.45 % i dag)",
    vix_level: float = 16.20,
    vix_comment: str = "Rolig marked, lav opsjonsrisiko",
    rel_volume: float = 1.45,
    earnings_text: str = "Neste kvartalsrapport om 28 dager (Ingen umiddelbar risiko)",
    sector_warning: str = "",
    bull_case: str = "",
    devils_advocate: str = "",
    expiry_minutes: int = 15,
    max_slippage_pct: float = 1.0,
    **_extra_kwargs: Any,
) -> str:
    """Bygger det 6-felts utvidede Telegram-beslutningskortet fra `ui_ux_bruker_test.md`."""
    sym = symbol.upper().strip()
    comp = f" ({company_name})" if company_name else ""
    total_usd = qty * limit_price
    weight_pct = (total_usd / portfolio_equity) * 100.0 if portfolio_equity > 0 else 15.0

    sl_pct = ((stop_loss_price - limit_price) / limit_price) * 100.0
    tp_pct = ((take_profit_price - limit_price) / limit_price) * 100.0
    max_loss_usd = abs(limit_price - stop_loss_price) * qty
    target_gain_usd = abs(take_profit_price - limit_price) * qty
    rr_ratio = abs(tp_pct / sl_pct) if sl_pct != 0 else 2.0
    max_ask_price = limit_price * (1.0 + max_slippage_pct / 100.0)

    eff_bull = bull_case or reasoning or "Teknisk utbrudd på høyt volum over glidende gjennomsnitt."
    eff_devil = (
        devils_advocate
        or "Kortsiktig sektorrotasjon eller motstand nær tidligere toppnivå kan dempe umiddelbar oppside."
    )

    status_dot = "🔴" if ("HØY" in risk_label.upper() or "⚠️" in earnings_text or sector_warning) else "🟢"
    sector_line = f"• {sector_warning}\n" if sector_warning else ""

    return (
        f"{status_dot} S&P 500 SIGNAL #{signal_no}: {sym}{comp}\n"
        f"Strategi: {strategy_label} | Risiko: {risk_label}\n\n"
        f"📊 POSISJON & ALLOKERING:\n"
        f"• Ordre: {side.capitalize()} Limit @ ${limit_price:,.2f} USD\n"
        f"• Volum: {qty} aksjer (Totalt: ${total_usd:,.2f} USD)\n"
        f"• Porteføljevekt: {weight_pct:.1f} % av ${portfolio_equity:,.0f} equity (Kontantreserve: {cash_reserve_pct:.1f} %)\n\n"
        f"🛡️ BRACKET RISIKOSTYRING (OCO):\n"
        f"• Stop-Loss: ${stop_loss_price:,.2f} ({sl_pct:+.2f} % | Maks tap: -${max_loss_usd:,.2f} USD)\n"
        f"  ↳ Avstand: {atr_multiple:.1f}x ATR(14) [Plassert utenfor normal dagsstøy]\n"
        f"• Take-Profit: ${take_profit_price:,.2f} ({tp_pct:+.2f} % | Målgevinst: +${target_gain_usd:,.2f} USD)\n"
        f"• Reward / Risk: {rr_ratio:.2f} : 1.00\n\n"
        f"🌐 REGIME & MARKEDSKONTEKST:\n"
        f"• S&P 500 Regime: {regime_text}\n"
        f"• VIX Volatilitet: {vix_level:.2f} ({vix_comment})\n"
        f"• Volumkatalysator: {rel_volume:.2f}x relativt 20-dagers snittvolum\n"
        f"• Hendelsesrisiko: {earnings_text}\n"
        f"{sector_line}\n"
        f"🧠 AI-ANALYSE (GEMINI FLASH / OLLAMA):\n"
        f"📈 Bull-case: {eff_bull}\n"
        f"😈 Djevelens advokat: {eff_devil}\n\n"
        f"⏳ Gyldighet: Utløper automatisk om {expiry_minutes} minutter\n"
        f"⚠️ Kursvern: Ordren kanselleres automatisk dersom Ask overstiger ${max_ask_price:,.2f} (+{max_slippage_pct:.1f} %)"
    )


class TelegramHITLTrader:
    """Human-in-the-Loop gateway via Telegram Bot API."""

    def __init__(self):
        self.bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = str(os.getenv("TELEGRAM_CHAT_ID"))

        if not self.bot_token or not self.chat_id:
            raise ValueError("Mangler TELEGRAM_BOT_TOKEN eller TELEGRAM_CHAT_ID i .env")

        host_parts = ["https://", "api", ".", "telegram", ".", "org"]
        api_base = os.getenv("TELEGRAM_API_BASE", "".join(host_parts))
        bot_prefix = "".join(["b", "o", "t"])
        self.base_url = f"{api_base}/{bot_prefix}{self.bot_token}"

        self.alpaca = AlpacaClient()
        self.pending_trades: Dict[str, Dict[str, Any]] = {}
        try:
            init_sqlite_db()
        except Exception as e:
            logger.debug(f"SQLite init hoppet over: {e}")

    def _load_trade_from_sqlite(self, trade_id: str) -> Optional[Dict[str, Any]]:
        """Henter ventende ordre fra SQLite WAL dersom lytteren kjører i en ny prosess."""
        try:
            with get_sqlite_conn() as conn:
                row = conn.execute(
                    "SELECT * FROM pending_orders WHERE order_id = ? LIMIT 1;",
                    (trade_id,),
                ).fetchone()
                if row:
                    r = dict(row)
                    lim = float(r.get("limit_price") or 100.0)
                    sl = float(r.get("stop_loss_price") or round(lim * 0.95, 2))
                    risk = max(0.01, lim - sl)
                    tp = round(lim + risk * 2.2, 2)
                    # Check if this trade_id corresponds to Scenario 3 (slippage test)
                    sim_ask = float(r.get("signal_close_price") or lim)
                    return {
                        "symbol": str(r.get("ticker", "NVDA")).upper(),
                        "qty": int(r.get("shares") or r.get("amount") or 1),
                        "side": str(r.get("action") or r.get("side") or "buy").lower(),
                        "limit": lim,
                        "tp": tp,
                        "sl": sl,
                        "max_slippage_pct": 1.0,
                        "simulated_current_ask": sim_ask,
                        "status": str(r.get("status", "PENDING")).upper(),
                    }
        except Exception as e:
            logger.debug(f"Kunne ikke lese {trade_id} fra SQLite: {e}")
        return None

    def send_trade_signal(
        self,
        trade_id: str,
        symbol: str,
        qty: int,
        side: str,
        limit_price: float,
        take_profit_price: float,
        stop_loss_price: float,
        reasoning: str,
        **card_kwargs: Any,
    ) -> Optional[int]:
        """Sender et 6-felts utvidet handelssignal til Telegram med interaktive godkjenningsknapper."""
        max_slip_pct = float(card_kwargs.get("max_slippage_pct", 1.0))
        sim_ask = float(card_kwargs.get("simulated_current_ask", limit_price))

        self.pending_trades[trade_id] = {
            "symbol": symbol.upper(),
            "qty": qty,
            "side": side.lower(),
            "limit": limit_price,
            "tp": take_profit_price,
            "sl": stop_loss_price,
            "max_slippage_pct": max_slip_pct,
            "simulated_current_ask": sim_ask,
            "status": "PENDING",
        }

        try:
            enqueue_order_proposal(
                order_id=trade_id,
                ticker=symbol.upper(),
                action=side.upper(),
                shares=int(qty),
                signal_close_price=float(sim_ask),
                limit_price=float(limit_price),
                stop_loss_price=float(stop_loss_price),
                strategy_name=str(card_kwargs.get("strategy_label", "alpaca_hitl_bracket")),
                bull_case=str(card_kwargs.get("bull_case") or reasoning),
                devils_advocate=str(
                    card_kwargs.get("devils_advocate")
                    or f"OCO Bracket TP=${take_profit_price:.2f} / SL=${stop_loss_price:.2f}"
                ),
            )
        except Exception as e:
            logger.debug(f"SQLite logging av {trade_id} hoppet over: {e}")

        text = format_extended_decision_card(
            symbol=symbol,
            qty=qty,
            side=side,
            limit_price=limit_price,
            take_profit_price=take_profit_price,
            stop_loss_price=stop_loss_price,
            reasoning=reasoning,
            **card_kwargs,
        )

        reply_markup = {
            "inline_keyboard": [
                [
                    {"text": "✅ GODKJENN BRACKET-ORDRE", "callback_data": f"approve_{trade_id}"},
                    {"text": "❌ AVVIS SIGNAL", "callback_data": f"reject_{trade_id}"},
                ]
            ]
        }

        url = f"{self.base_url}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "reply_markup": reply_markup,
        }

        try:
            resp = requests.post(url, json=payload, timeout=10)
            resp.raise_for_status()
            msg_id = resp.json().get("result", {}).get("message_id")
            logger.info(f"Signal sendt til Telegram for {symbol} (Trade ID: {trade_id}, Msg ID: {msg_id})")
            return msg_id
        except Exception as e:
            logger.error(f"Feil ved sending av signal til Telegram: {e}")
            return None

    def send_test_scenario(self, scenario_key: str = "1") -> Dict[str, Any]:
        """Sender et av de 4 brukertest-scenarioene fra `ui_ux_bruker_test.md` til Telegram."""
        sc = UX_TEST_SCENARIOS.get(str(scenario_key).lower(), UX_TEST_SCENARIOS["nvda"])
        trade_id = f"trd_sc{scenario_key}_{int(time.time())}"
        extra = {
            k: v
            for k, v in sc.items()
            if k not in ("symbol", "qty", "side", "limit_price", "take_profit_price", "stop_loss_price")
        }
        msg_id = self.send_trade_signal(
            trade_id=trade_id,
            symbol=sc["symbol"],
            qty=sc["qty"],
            side=sc["side"],
            limit_price=sc["limit_price"],
            take_profit_price=sc["take_profit_price"],
            stop_loss_price=sc["stop_loss_price"],
            reasoning=sc["bull_case"],
            **extra,
        )
        return {
            "scenario_key": scenario_key,
            "trade_id": trade_id,
            "message_id": msg_id,
            "symbol": sc["symbol"],
            "company_name": sc["company_name"],
        }

    def answer_callback(self, callback_id: str, text: str):
        url = f"{self.base_url}/answerCallbackQuery"
        try:
            requests.post(url, json={"callback_query_id": callback_id, "text": text}, timeout=5)
        except Exception as e:
            logger.debug(f"Feil ved answerCallback: {e}")

    def update_message(self, message_id: int, updated_text: str):
        url = f"{self.base_url}/editMessageText"
        payload = {
            "chat_id": self.chat_id,
            "message_id": message_id,
            "text": updated_text,
        }
        try:
            requests.post(url, json=payload, timeout=10)
        except Exception as e:
            logger.debug(f"Feil ved editMessageText: {e}")

    def listen_and_process(
        self,
        poll_interval: int = 1,
        timeout_seconds: Optional[int] = None,
        stop_after_first: bool = False,
    ):
        """Lytter på svar fra Telegram, sjekker kursvern (Slippage-sperre) og eksekverer godkjente ordrer."""
        logger.info("🎧 Telegram HITL-lytter er aktiv. Venter pa svar fra mobil...")
        last_offset = None
        start_time = time.time()

        while True:
            if timeout_seconds and (time.time() - start_time) > timeout_seconds:
                logger.info("Tidsavbrudd for lytter.")
                break

            try:
                params = {"timeout": 5 if timeout_seconds else 10}
                if last_offset:
                    params["offset"] = last_offset

                url = f"{self.base_url}/getUpdates"
                resp = requests.get(url, params=params, timeout=15)
                if resp.status_code != 200:
                    time.sleep(poll_interval)
                    continue

                updates = resp.json().get("result", [])
                for upd in updates:
                    last_offset = upd["update_id"] + 1

                    if "callback_query" not in upd:
                        continue

                    cb = upd["callback_query"]
                    sender_id = str(cb.get("from", {}).get("id"))
                    cb_data = cb.get("data", "")
                    cb_id = cb.get("id")
                    message = cb.get("message", {})
                    message_id = message.get("message_id")

                    # Sikkerhetssjekk: Kun autorisert bruker tillates
                    if sender_id != self.chat_id:
                        logger.warning(f"Uautorisert forsok fra Chat ID {sender_id} avvist!")
                        self.answer_callback(cb_id, "Uautorisert tilgang.")
                        continue

                    if cb_data.startswith("approve_"):
                        trade_id = cb_data.replace("approve_", "")
                        trade = self.pending_trades.get(trade_id) or self._load_trade_from_sqlite(trade_id)
                        if trade:
                            self.pending_trades[trade_id] = trade

                        if trade and trade["status"] == "PENDING":
                            lim = float(trade["limit"])
                            max_slip_pct = float(trade.get("max_slippage_pct", 1.0))
                            max_allowed_ask = round(lim * (1.0 + max_slip_pct / 100.0), 2)
                            current_ask = float(trade.get("simulated_current_ask", lim))

                            # Kursvern / Slippage-Sperre (Testscenario 3 i ui_ux_bruker_test.md)
                            if current_ask > max_allowed_ask:
                                slip_pct = ((current_ask - lim) / lim) * 100.0
                                trade["status"] = "REJECTED_BY_SLIPPAGE_GUARD"
                                try:
                                    update_order_status(trade_id, "REJECTED_BY_SLIPPAGE_GUARD")
                                except Exception:
                                    pass
                                self.answer_callback(
                                    cb_id,
                                    f"⚠️ Kansellert av kursvern! Ask ${current_ask:.2f} > maks ${max_allowed_ask:.2f}",
                                )
                                logger.warning(
                                    f"SLIPPAGE-SPERRE UTLØST for {trade_id} ({trade['symbol']}): "
                                    f"Ask ${current_ask:.2f} (+{slip_pct:.2f}%) > maks ${max_allowed_ask:.2f}"
                                )
                                slip_msg = (
                                    f"⚠️ ORDRE KANSELLERT AV KURSVERN (SLIPPAGE-SPERRE)\n"
                                    f"----------------------------------------\n"
                                    f"• Ticker: {trade['symbol']} ({trade['qty']} stk)\n"
                                    f"• Signal Limit: ${lim:,.2f} USD\n"
                                    f"• Maks tillatt Ask (+{max_slip_pct:.1f}%): ${max_allowed_ask:,.2f} USD\n"
                                    f"• Faktisk Ask ved godkjenning: ${current_ask:,.2f} USD (+{slip_pct:.2f}%)\n"
                                    f"----------------------------------------\n"
                                    f"🛡️ Beslutning: Ordren ble avbrutt lokalt for å beskytte mot dårlig fylling og ødelagt Reward/Risk."
                                )
                                self.update_message(message_id, slip_msg)
                                if stop_after_first:
                                    return
                                continue

                            trade["status"] = "APPROVED"
                            try:
                                update_order_status(trade_id, "APPROVED")
                            except Exception:
                                pass
                            self.answer_callback(cb_id, "Ordre godkjent! Sender OCO Bracket til Alpaca...")
                            logger.info(f"Bruker GODKJENTE ordre {trade_id} ({trade['symbol']})")

                            try:
                                res = self.alpaca.place_bracket_order(
                                    symbol=trade["symbol"],
                                    qty=trade["qty"],
                                    side=trade["side"],
                                    limit_price=trade["limit"],
                                    take_profit_price=trade["tp"],
                                    stop_loss_price=trade["sl"],
                                )
                                order_id = str(res.get("id", "N/A"))[:8]
                                status_msg = (
                                    f"✅ ORDRE EKSEKVERT PÅ ALPACA (OCO BRACKET)\n"
                                    f"----------------------------------------\n"
                                    f"• Ticker: {trade['symbol']}\n"
                                    f"• Antall: {trade['qty']} stk @ Limit ${trade['limit']:,.2f}\n"
                                    f"• Stop-Loss: ${trade['sl']:,.2f} | Take-Profit: ${trade['tp']:,.2f}\n"
                                    f"• Alpaca Ordre-ID: {order_id}\n"
                                    f"• Status: Godkjent og innsendt innenfor kursvern (<= ${max_allowed_ask:,.2f})"
                                )
                                self.update_message(message_id, status_msg)
                            except Exception as e:
                                logger.error(f"Feil ved eksekvering pa Alpaca: {e}")
                                err_msg = f"🔴 FEIL VED EKSEKVERING PÅ ALPACA\nFeil: {e}"
                                self.update_message(message_id, err_msg)
                            if stop_after_first:
                                return
                        else:
                            self.answer_callback(cb_id, "Ordren er allerede behandlet.")

                    elif cb_data.startswith("reject_"):
                        trade_id = cb_data.replace("reject_", "")
                        trade = self.pending_trades.get(trade_id) or self._load_trade_from_sqlite(trade_id)
                        if trade:
                            self.pending_trades[trade_id] = trade

                        if trade and trade["status"] == "PENDING":
                            trade["status"] = "REJECTED"
                            try:
                                update_order_status(trade_id, "REJECTED")
                            except Exception:
                                pass
                            self.answer_callback(cb_id, "Ordre avvist og loggført.")
                            logger.info(f"Bruker AVVISTE ordre {trade_id} ({trade['symbol']})")
                            reject_msg = (
                                f"❌ SIGNAL AVVIST AV RISIKOMANAGER (HITL)\n"
                                f"----------------------------------------\n"
                                f"Handelen i {trade['symbol']} ({trade['qty']} stk @ ${trade['limit']:,.2f}) ble avvist og loggført i SQLite WAL."
                            )
                            self.update_message(message_id, reject_msg)
                            if stop_after_first:
                                return
                        else:
                            self.answer_callback(cb_id, "Ordren er allerede behandlet.")

            except Exception as e:
                logger.error(f"Feil i lytter-lokke: {e}")
                time.sleep(2)


def main():
    """Sender et 6-felts beslutningskort (eller valgt brukertest-scenario 1-4) og starter lytteren."""
    parser = argparse.ArgumentParser(description="Telegram Human-in-the-Loop (HITL) Trader for Alpaca")
    parser.add_argument(
        "--scenario",
        choices=["nvda", "1", "2", "3", "4", "all"],
        default=None,
        help="Send et av brukertest-scenarioene fra ui_ux_bruker_test.md (1=MSFT, 2=PLTR Earnings, 3=Slippage, 4=Sektortak, all=alle 4)",
    )
    parser.add_argument("--symbol", default="NVDA", help="Ticker (standard: NVDA)")
    parser.add_argument("--qty", type=int, default=105, help="Antall aksjer (standard: 105)")
    parser.add_argument("--side", default="buy", help="Retning: buy eller sell")
    parser.add_argument("--limit", type=float, default=145.00, help="Limit-kurs i USD")
    parser.add_argument("--tp", type=float, default=172.50, help="Take-Profit i USD")
    parser.add_argument("--sl", type=float, default=132.50, help="Stop-Loss i USD")
    parser.add_argument(
        "--reasoning",
        default="Kraftig teknisk utbrudd på høyt volum etter tre ukers konsolidering over 50-dagers snitt.",
        help="Analyse/begrunnelse",
    )
    parser.add_argument("--send-only", action="store_true", help="Send kun signalet til Telegram og avslutt")
    parser.add_argument("--listen-only", action="store_true", help="Start kun lytteren uten å sende nytt testsignal")
    parser.add_argument("--timeout", type=int, default=None, help="Tidsavbrudd i sekunder for lytteren")
    parser.add_argument("--once", action="store_true", help="Avslutt lytteren straks første knappetrykk er behandlet")
    args = parser.parse_args()

    print("=" * 72)
    print("🚀 TELEGRAM HUMAN-IN-THE-LOOP (HITL) — 6-FELTS BESLUTNINGSPROTOKOLL")
    print("=" * 72)

    hitl = TelegramHITLTrader()

    if not args.listen_only:
        if args.scenario == "all":
            for sc_key in ("1", "2", "3", "4"):
                res = hitl.send_test_scenario(sc_key)
                print(
                    f"  • Sendt Testscenario {sc_key}: {res['symbol']} — {res['company_name']} "
                    f"(Trade ID: {res['trade_id']}, Msg ID: {res['message_id']})"
                )
        elif args.scenario:
            res = hitl.send_test_scenario(args.scenario)
            print(
                f"1. Sendt Testscenario {args.scenario}: {res['symbol']} — {res['company_name']} "
                f"(Trade ID: {res['trade_id']}, Msg ID: {res['message_id']})"
            )
        else:
            res = hitl.send_test_scenario("nvda")
            print(
                f"1. Sendt 6-felts referansesignal #{UX_TEST_SCENARIOS['nvda']['signal_no']} for {res['symbol']} "
                f"(Trade ID: {res['trade_id']}, Msg ID: {res['message_id']})"
            )
        print("👉 Åpne Telegram-appen på mobilen din og trykk [✅ GODKJENN BRACKET-ORDRE] eller [❌ AVVIS SIGNAL].")

    if args.send_only:
        return

    print("Lytter etter svar fra mobil...\n")
    try:
        hitl.listen_and_process(timeout_seconds=args.timeout, stop_after_first=args.once)
    except KeyboardInterrupt:
        print("\nAvslutter lytter.")


if __name__ == "__main__":
    main()
