#!/usr/bin/env python3
"""
us_intraday_options_agent.py
=============================================================================
Toveis US Intradag Opsjonsagent (Long Calls & Long Puts).
Styrt av en streng Hurdle Rate Gatekeeper:
Gevinst MÅ overstige:
1. Opsjons Bid/Ask spread (typisk $0.05 - $0.15 per kontrakt).
2. Megler- & regulatoriske gebyrer (Alpaca $0.00 kurtasje, men OCC/ORF/FINRA avgifter).
3. 37.84% norsk kapitalskatt på gevinster.
4. Absolutt krav: Netto forventet gevinst etter alt >= 0.75% av kontraktpremie (og R/R >= 2.0x).
=============================================================================
"""

from __future__ import annotations

import logging
from typing import Any, Dict

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("USIntradayOptionsAgent")


class USIntradayOptionsAgent:
    def __init__(self, portfolio_value_usd: float = 50000.0, available_cash_usd: float = 12500.0):
        self.portfolio_value_usd = float(portfolio_value_usd)
        self.available_cash_usd = float(available_cash_usd)

        # Allokering: Maks 3.0% av porteføljeverdi for opsjoner (pga innebygd giring)
        self.max_allocation_pct = 0.03
        self.max_order_usd = min(self.portfolio_value_usd * self.max_allocation_pct, max(0.0, self.available_cash_usd))

        # Friksjon og Skatt
        self.norwegian_tax_rate = 0.3784
        self.option_half_spread_pct = 0.025
        self.clearing_fee_per_contract = 0.055  # OCC ($0.02) + ORF ($0.035) per kontrakt
        self.sec_fee_rate = 0.0000278

        # Hurdle Terskel for Opsjoner
        self.min_net_profit_pct = 0.0075
        self.min_risk_reward_ratio = 2.0

    def evaluate_option_signal(
        self,
        ticker: str,
        side: str,  # "BUY_CALL" eller "BUY_PUT"
        underlying_price: float,
        underlying_stop: float,
        underlying_target: float,
        contract_strike: float,
        contract_ask: float,
        contract_bid: float,
        delta: float,
        days_to_expiry: int = 5,
    ) -> Dict[str, Any]:
        """Vurderer et toveis opsjonssignal mot spread, avgifter og 37.84% norsk skatt."""
        is_call = side.upper() == "BUY_CALL"
        abs_delta = abs(float(delta))
        contract_cost_usd = float(contract_ask) * 100.0

        if contract_cost_usd <= 0 or contract_cost_usd > self.max_order_usd:
            return {
                "decision": "VETO_CAPITAL_EXCEEDED",
                "ticker": ticker,
                "side": side,
                "reason": f"Kontraktpris (${contract_cost_usd:.2f}) overstiger maks allokering (${self.max_order_usd:.2f})",
            }

        num_contracts = max(1, int(self.max_order_usd // contract_cost_usd))
        total_entry_cost = num_contracts * contract_cost_usd

        if is_call:
            underlying_risk = underlying_price - underlying_stop
            underlying_reward = underlying_target - underlying_price
        else:
            underlying_risk = underlying_stop - underlying_price
            underlying_reward = underlying_price - underlying_target

        if underlying_risk <= 0 or underlying_reward <= 0:
            return {
                "decision": "VETO_INVALID_LEVELS",
                "ticker": ticker,
                "side": side,
                "reason": "Stop eller Target er feilplassert ift kursretning.",
            }

        expected_option_loss_per_share = underlying_risk * abs_delta
        expected_exit_stop_price = max(0.01, contract_bid - expected_option_loss_per_share)

        expected_option_gain_per_share = underlying_reward * abs_delta
        expected_exit_target_price = contract_bid + expected_option_gain_per_share

        gross_profit_usd = (expected_exit_target_price - contract_ask) * 100.0 * num_contracts
        gross_loss_usd = (contract_ask - expected_exit_stop_price) * 100.0 * num_contracts

        spread_friction_usd = (contract_ask - contract_bid) * 100.0 * num_contracts
        regulatory_fees_usd = (self.clearing_fee_per_contract * 2 * num_contracts) + (
            self.sec_fee_rate * expected_exit_target_price * 100.0 * num_contracts
        )

        net_profit_before_tax = gross_profit_usd - regulatory_fees_usd
        estimated_tax_usd = max(0.0, net_profit_before_tax * self.norwegian_tax_rate)
        net_profit_after_tax = net_profit_before_tax - estimated_tax_usd

        net_profit_pct = net_profit_after_tax / total_entry_cost if total_entry_cost > 0 else 0.0
        rr_ratio = gross_profit_usd / gross_loss_usd if gross_loss_usd > 0 else 0.0

        passed_hurdle = (net_profit_pct >= self.min_net_profit_pct) and (rr_ratio >= self.min_risk_reward_ratio)
        decision = "APPROVED_FOR_EXECUTION" if passed_hurdle else "VETO_HURDLE_OR_FRICTION"

        return {
            "decision": decision,
            "ticker": ticker.upper(),
            "side": side.upper(),
            "instrument": f"{ticker.upper()} {'CALL' if is_call else 'PUT'} K={contract_strike} (DTE: {days_to_expiry}d)",
            "num_contracts": num_contracts,
            "total_capital_committed_usd": round(total_entry_cost, 2),
            "contract_ask": contract_ask,
            "contract_bid": contract_bid,
            "expected_target_price": round(expected_exit_target_price, 2),
            "expected_stop_price": round(expected_exit_stop_price, 2),
            "risk_reward_ratio": round(rr_ratio, 2),
            "spread_friction_usd": round(spread_friction_usd, 2),
            "regulatory_fees_usd": round(regulatory_fees_usd, 2),
            "gross_profit_usd": round(gross_profit_usd, 2),
            "net_profit_after_tax_usd": round(net_profit_after_tax, 2),
            "net_return_after_tax_pct": round(net_profit_pct * 100.0, 2),
            "tax_drag_usd": round(estimated_tax_usd, 2),
        }


if __name__ == "__main__":
    agent = USIntradayOptionsAgent(portfolio_value_usd=50000.0, available_cash_usd=15000.0)
    print(agent.evaluate_option_signal("NVDA", "BUY_CALL", 145.0, 143.2, 149.5, 144.0, 4.15, 4.05, 0.68, 4))
    print(agent.evaluate_option_signal("TSLA", "BUY_PUT", 210.0, 213.5, 201.0, 212.5, 6.20, 6.05, -0.65, 4))
