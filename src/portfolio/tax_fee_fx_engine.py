"""Deterministic Tax, Fee & FX Routing and Trade Hurdle Engine (`src/portfolio/tax_fee_fx_engine.py`).

Implements the formal architecture from `docs/anbefalte_opt_tax_fee_fx_struktur.md`:
1. Strategic market-to-account/broker routing (Nordnet ASK for NO/SE/DK/FI/EEA,
   Alpaca Markets USD Pool for US Mega-Cap & algorithmic US trading, Saxo Bank for Global ex-US/Nordic).
2. 7-component All-In round-trip friction model:
   C_all_in = C_commission + C_spread + C_slippage + C_FX + C_regulatory + C_custody + C_tax_drag
3. Hard Trade Hurdle Gate:
   E[Alpha_gross] > C_all_in + Alpha_buffer
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional

# Norwegian 2026 effective equity capital gains tax rate (after 1.72x adjustment factor)
NORWAY_EQUITY_TAX_RATE = 0.3784

BROKER_PROFILES: Dict[str, Dict[str, Any]] = {
    "alpaca_usd_pool": {
        "label": "Alpaca Markets (Dedikert USD-pool)",
        "commission_rate": 0.0,
        "commission_floor": 0.0,
        "fx_rate_per_side": 0.0,
        "regulatory_round_trip_pct": 0.003,  # SEC + FINRA TAF + CAT (~0.3 bps)
        "custody_annual_pct": 0.0,
        "ask_eligible": False,
    },
    "nordnet_ask_normal": {
        "label": "Nordnet ASK (Normal: 0.049%, min 79 NOK)",
        "commission_rate": 0.00049,
        "commission_floor": 79.0,
        "fx_rate_per_side": 0.0025,
        "regulatory_round_trip_pct": 0.0,
        "custody_annual_pct": 0.0,
        "ask_eligible": True,
    },
    "nordnet_ask_mini": {
        "label": "Nordnet ASK (Mini: 0.15%, min 29 NOK)",
        "commission_rate": 0.0015,
        "commission_floor": 29.0,
        "fx_rate_per_side": 0.0025,
        "regulatory_round_trip_pct": 0.0,
        "custody_annual_pct": 0.0,
        "ask_eligible": True,
    },
    "saxo_classic": {
        "label": "Saxo Bank Classic (0.08%, min 10 NOK, 0.12% depot)",
        "commission_rate": 0.0008,
        "commission_floor": 10.0,
        "fx_rate_per_side": 0.0025,
        "regulatory_round_trip_pct": 0.0,
        "custody_annual_pct": 0.12,
        "ask_eligible": True,
    },
    "saxo_vip": {
        "label": "Saxo Bank VIP (0.03%, 0.15% manuell FX, 0.12% depot)",
        "commission_rate": 0.0003,
        "commission_floor": 10.0,
        "fx_rate_per_side": 0.0015,
        "regulatory_round_trip_pct": 0.0,
        "custody_annual_pct": 0.12,
        "ask_eligible": True,
    },
}


@dataclass(frozen=True)
class RoutingDecision:
    ticker: str
    market_segment: str
    recommended_account: str
    recommended_broker_key: str
    ask_qualified: bool
    native_currency: str
    rationale: str


def route_instrument(
    ticker: str,
    is_adr: bool = False,
    eea_domiciled: Optional[bool] = None,
) -> RoutingDecision:
    """Deterministically route an instrument to the optimal account & broker."""
    sym = (ticker or "").strip().upper()

    if sym.endswith(".OL"):
        return RoutingDecision(
            ticker=sym,
            market_segment="Norge (OBX 15 / Oslo Børs)",
            recommended_account="Nordnet ASK (OSE)",
            recommended_broker_key="nordnet_ask_mini",
            ask_qualified=True,
            native_currency="NOK",
            rationale="Hjemmemarked i NOK uten FX-spread; utsatt gevinstskatt (0% løpende) via ASK.",
        )
    if sym.endswith(".ST"):
        return RoutingDecision(
            ticker=sym,
            market_segment="Norden (OMXN40 / Nasdaq Stockholm)",
            recommended_account="Nordnet ASK (Stockholm)",
            recommended_broker_key="nordnet_ask_normal",
            ask_qualified=True,
            native_currency="SEK",
            rationale="EØS-kvalifisert for ASK; 0% løpende gevinstskatt overstiger FX-friksjon over tid.",
        )
    if sym.endswith(".CO"):
        return RoutingDecision(
            ticker=sym,
            market_segment="Norden (OMXN40 / Nasdaq Copenhagen)",
            recommended_account="Nordnet ASK / Saxo ASK (København)",
            recommended_broker_key="saxo_classic",
            ask_qualified=True,
            native_currency="DKK",
            rationale="EØS-kvalifisert for ASK; Saxo har lavere minstekurtasje på ordrer under 20 000 DKK.",
        )
    if sym.endswith(".HE"):
        return RoutingDecision(
            ticker=sym,
            market_segment="Norden (OMXN40 / Nasdaq Helsinki)",
            recommended_account="Nordnet ASK (Helsinki)",
            recommended_broker_key="nordnet_ask_normal",
            ask_qualified=True,
            native_currency="EUR",
            rationale="EØS-kvalifisert for ASK med automatisk skatterapportering i EUR.",
        )
    if "." in sym:
        # Other international exchange (e.g. .L, .SW, .T, .DE)
        is_eea = bool(eea_domiciled) if eea_domiciled is not None else sym.endswith((".DE", ".PA", ".AS", ".MI"))
        if is_eea:
            return RoutingDecision(
                ticker=sym,
                market_segment="Europa (EØS)",
                recommended_account="Nordnet ASK (EØS)",
                recommended_broker_key="nordnet_ask_normal",
                ask_qualified=True,
                native_currency="EUR",
                rationale="EØS-aksje kvalifisert for ASK; skatteutsettelse dominerer transaksjonsfriksjon.",
            )
        return RoutingDecision(
            ticker=sym,
            market_segment="Globale markeder (utenfor USA/Norden)",
            recommended_account="Saxo Bank (Multi-valuta)",
            recommended_broker_key="saxo_classic",
            ask_qualified=False,
            native_currency="LOCAL",
            rationale="Tilgang til 50+ globale børser (obs: 0.12% årlig depotgebyr på norske depoter).",
        )

    # US ticker (NYSE / Nasdaq)
    if is_adr:
        return RoutingDecision(
            ticker=sym,
            market_segment="US ADR (NYSE / Nasdaq)",
            recommended_account="Unngå ADR som standard (bruk primærnotering / ASK hvis EØS)",
            recommended_broker_key="alpaca_usd_pool",
            ask_qualified=False,
            native_currency="USD",
            rationale="ADR tilfører ekstra depotgebyr og mister ASK-fordel for EØS-selskaper.",
        )

    return RoutingDecision(
        ticker=sym,
        market_segment="US Mega-Cap / Algoritmisk US-trading (NYSE / Nasdaq)",
        recommended_account="Alpaca Markets (Dedikert USD-pool)",
        recommended_broker_key="alpaca_usd_pool",
        ask_qualified=False,
        native_currency="USD",
        rationale="$0 provisjon, OCO bracket-ordrer og dedikert USD-buffer eliminerer tur-retur FX.",
    )


def evaluate_trade_hurdle(
    ticker: str,
    trade_value: float,
    expected_alpha_pct: float,
    broker_key: Optional[str] = None,
    spread_pct: float = 0.05,
    slippage_pct: float = 0.05,
    alpha_buffer_pct: float = 0.20,
    holding_days: int = 10,
    fx_mode: str = "auto",
    is_adr: bool = False,
) -> Dict[str, Any]:
    """Compute exact 7-component round-trip All-In Cost and evaluate the Trade Hurdle Rule.

    Trade Condition: E[Alpha_gross] > C_all_in + Alpha_buffer
    """
    val = max(float(trade_value), 1.0)
    gross_alpha = float(expected_alpha_pct)
    routing = route_instrument(ticker, is_adr=is_adr)
    chosen_broker = broker_key or routing.recommended_broker_key
    prof = BROKER_PROFILES.get(chosen_broker, BROKER_PROFILES["alpaca_usd_pool"])

    # 1. Round-trip broker commission (%)
    one_side_comm = max(val * float(prof["commission_rate"]), float(prof["commission_floor"]))
    commission_pct = ((2.0 * one_side_comm) / val) * 100.0 if prof["commission_rate"] > 0 or prof["commission_floor"] > 0 else 0.0

    # 2. Bid-Ask spread (%)
    c_spread_pct = max(0.0, float(spread_pct))

    # 3. Execution slippage (%)
    c_slippage_pct = max(0.0, float(slippage_pct))

    # 4. FX friction round-trip (%)
    if routing.native_currency == "NOK" or chosen_broker == "alpaca_usd_pool" or fx_mode == "usd_pool":
        c_fx_pct = 0.0
    elif fx_mode == "manual_subaccount":
        c_fx_pct = 0.15 * 2.0  # 0.30% round-trip
    else:
        c_fx_pct = float(prof["fx_rate_per_side"]) * 2.0 * 100.0

    # 5. Regulatory fees (%)
    c_reg_pct = float(prof["regulatory_round_trip_pct"])

    # 6. Pro-rated annual custody fee (%)
    c_custody_pct = float(prof["custody_annual_pct"]) * (max(holding_days, 1) / 365.0)

    # 7. Tax timing drag (%)
    # Inside ASK -> 0% immediate tax drag; outside ASK -> compounding drag on positive realized gains
    in_ask = bool(routing.ask_qualified and prof["ask_eligible"])
    if in_ask:
        c_tax_drag_pct = 0.0
    else:
        # Annualized opportunity-cost drag of paying 37.84% tax immediately vs. deferring in ASK
        # (modeled as ~5% opportunity cost on the 37.84% tax outflow scaled by holding horizon)
        pre_tax_gain = max(0.0, gross_alpha - (commission_pct + c_spread_pct + c_slippage_pct + c_fx_pct + c_reg_pct + c_custody_pct))
        c_tax_drag_pct = pre_tax_gain * NORWAY_EQUITY_TAX_RATE * 0.05

    all_in_cost_pct = commission_pct + c_spread_pct + c_slippage_pct + c_fx_pct + c_reg_pct + c_custody_pct + c_tax_drag_pct
    hurdle_pct = all_in_cost_pct + float(alpha_buffer_pct)
    net_alpha_pct = gross_alpha - all_in_cost_pct
    passed = gross_alpha > hurdle_pct

    return {
        "ticker": routing.ticker,
        "routing": asdict(routing),
        "broker_key": chosen_broker,
        "broker_label": prof["label"],
        "in_ask": in_ask,
        "trade_value": round(val, 2),
        "expected_alpha_pct": round(gross_alpha, 4),
        "alpha_buffer_pct": round(float(alpha_buffer_pct), 4),
        "costs_pct": {
            "commission": round(commission_pct, 4),
            "spread": round(c_spread_pct, 4),
            "slippage": round(c_slippage_pct, 4),
            "fx": round(c_fx_pct, 4),
            "regulatory": round(c_reg_pct, 4),
            "custody": round(c_custody_pct, 4),
            "tax_drag": round(c_tax_drag_pct, 4),
        },
        "all_in_cost_pct": round(all_in_cost_pct, 4),
        "hurdle_pct": round(hurdle_pct, 4),
        "net_alpha_pct": round(net_alpha_pct, 4),
        "passed": passed,
        "decision": "PASS -> SEND TIL TELEGRAM HITL" if passed else "REJECT -> AVVIST LOKALT (UNDER HURDLE)",
    }
