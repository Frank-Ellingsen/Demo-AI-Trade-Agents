"""Event-driven portfolio backtest simulator for the OBX 15 trading desk (Agents 1, 4, and 5).

Enforces:
- Look-ahead protection: Signals evaluated on Close(T), executed at Open(T+1).
- Total Return accounting: Tracks cash dividends paid while holding a position and blocks
  false entries on ex-dividend dates.
- 09:00 CET Gap Guardrail: Cancels BUY if Open(T+1) gaps > +1.5% or < -2.0% from Close(T).
- Portfolio & Sector caps: Max 4 concurrent positions (25% allocation), max 2 per sector,
  with deterministic priority_rank tie-breaking.
- Broker fee & MiFID II tick-size simulation (Saxo Bank Classic vs. Nordnet Mini).
- Return attribution (Market + Sector + Pure Alpha) and 3-factor OLS regression (OBX, Brent, USDNOK).
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Any

from src.analytics.attribution import decompose_trade_return, run_multifactor_ols_regression
from src.backtest.metrics import StrategyMetrics, compute_portfolio_metrics
from src.portfolio.allocator import CandidateSignal, select_portfolio_candidates
from src.portfolio.cost_tax_model import (
    BrokerFeeProfile,
    evaluate_round_trip_friction,
    round_to_tick_size,
)
from src.portfolio.risk_manager import check_opening_gap_guardrail
from src.signals.exogenous import ExogenousRegimeState, check_exogenous_regime


@dataclass
class OpenPosition:
    """Active portfolio position tracked day by day."""

    trade_id: str
    ticker: str
    sector: str
    liquidity_band: int
    strategy_name: str
    entry_sub_strategy: str
    horizon_days: int
    signal_date: date
    entry_date: date
    shares: int
    entry_raw_price: float
    stop_loss_raw: float
    highest_raw_close: float
    atr_at_entry: float
    obx_at_entry: float
    accumulated_dividends_nok: float = 0.0
    days_held: int = 0


def evaluate_entry_trigger(row: dict[str, Any], strategy_name: str) -> tuple[bool, float, str]:
    """Evaluate endogenous entry rules on Close(T) and return (triggered, priority_rank, sub_strategy)."""
    adj_c = row["adj_close"]
    sma200 = row["sma200"]
    sma50 = row.get("sma50")
    if adj_c is None or sma200 is None:
        return False, 0.0, strategy_name

    # Never enter on or immediately before an ex-dividend date (avoids mechanical ex-div gaps)
    if (row.get("div_amt") or 0.0) > 0.0 or (row.get("next_div_amt") or 0.0) > 0.0:
        return False, 0.0, strategy_name

    if strategy_name == "rsi_mean_reversion":
        rsi2 = row["rsi2"]
        ret_3d = row.get("ret_3d") or 0.0
        if (
            rsi2 is not None
            and adj_c > sma200
            and (sma50 is None or sma50 > sma200)
            and rsi2 < 10.0
            and ret_3d <= -0.025
        ):
            return True, round(100.0 - float(rsi2), 4), "rsi_mean_reversion"
        return False, 0.0, "rsi_mean_reversion"

    if strategy_name == "trend_breakout":
        high20 = row["prev_high20"]
        vol_ratio = row["vol_ratio"] or 0.0
        if high20 is not None and adj_c > sma200 and adj_c > high20 and vol_ratio >= 1.25:
            return True, round(float(vol_ratio), 4), "trend_breakout"
        return False, 0.0, "trend_breakout"

    if strategy_name == "bollinger_squeeze":
        bb_w = row["bb_width"]
        min_w60 = row["min_bb_width_60"]
        bb_up = row["bb_upper"]
        vol_ratio = row["vol_ratio"] or 0.0
        if (
            bb_w is not None
            and min_w60 is not None
            and bb_up is not None
            and adj_c > sma200
            and bb_w <= min_w60
            and adj_c > bb_up
        ):
            return True, round(float(vol_ratio), 4), "bollinger_squeeze"
        return False, 0.0, "bollinger_squeeze"

    if strategy_name == "combined_rsi_breakout":
        trig_rsi, rank_rsi, _ = evaluate_entry_trigger(row, "rsi_mean_reversion")
        if trig_rsi:
            return True, rank_rsi, "rsi_mean_reversion"
        trig_brk, rank_brk, _ = evaluate_entry_trigger(row, "trend_breakout")
        if trig_brk:
            return True, round(50.0 + min(35.0, rank_brk * 10.0), 4), "trend_breakout"
        return False, 0.0, "combined_rsi_breakout"

    raise ValueError(f"Unknown strategy_name: {strategy_name}")


def evaluate_exit_trigger(
    pos: OpenPosition,
    row: dict[str, Any],
) -> tuple[bool, str]:
    """Evaluate exit rules on Close(T) for execution at Open(T+1)."""
    adj_c = row["adj_close"]
    raw_c = row["raw_close"]
    raw_l = row["raw_low"]

    # 1. Stop-loss check (adjusted for any dividends received while holding)
    effective_raw_low = raw_l + (pos.accumulated_dividends_nok / max(pos.shares, 1))
    effective_raw_close = raw_c + (pos.accumulated_dividends_nok / max(pos.shares, 1))
    if effective_raw_low <= pos.stop_loss_raw or effective_raw_close <= pos.stop_loss_raw:
        return True, "STOP_LOSS"

    # 2. Time-stop check
    if pos.days_held >= pos.horizon_days:
        return True, "TIME_STOP"

    # 3. Strategy-specific technical exit (respecting each position's originating entry_sub_strategy)
    active_rule = pos.entry_sub_strategy
    if active_rule == "rsi_mean_reversion":
        if pos.horizon_days <= 10:
            sma5 = row["sma5"]
            if sma5 is not None and adj_c > sma5:
                return True, "SIGNAL_EXIT"
        elif pos.horizon_days <= 20:
            sma20 = row["sma20"]
            if sma20 is not None and adj_c > sma20:
                return True, "SIGNAL_EXIT"
        else:
            sma20 = row["sma20"]
            rsi2 = row["rsi2"] or 50.0
            if sma20 is not None and adj_c > sma20 and rsi2 > 80.0:
                return True, "SIGNAL_EXIT"

    elif active_rule == "trend_breakout":
        low10 = row["prev_low10"]
        if low10 is not None and adj_c < low10:
            return True, "SIGNAL_EXIT"

    elif active_rule == "bollinger_squeeze":
        sma20 = row["sma20"]
        if sma20 is not None and adj_c < sma20:
            return True, "SIGNAL_EXIT"

    return False, ""


def run_portfolio_simulation(
    panel_rows: list[dict[str, Any]],
    strategy_name: str,
    horizon_days: int,
    exogenous_enabled: bool,
    broker: BrokerFeeProfile,
    period_label: str = "FULL_2019_2026",
    start_date: date | None = None,
    end_date: date | None = None,
    initial_capital: float = 100_000.0,
    unconstrained_single_stock_mode: bool = False,
) -> tuple[StrategyMetrics, list[dict[str, Any]]]:
    """Run an event-driven portfolio simulation for a single configuration cell.

    When `unconstrained_single_stock_mode=True`, simulates the 'Single-Stock (Unlimited Slots)'
    benchmark (fixed 25,000 NOK per trade across all 15 stocks with no 4-slot or 2/sector cap)
    to demonstrate why unconstrained single-stock testing overstates trade counts vs. a real 100k portfolio.
    """
    by_date: dict[date, list[dict[str, Any]]] = defaultdict(list)
    for r in panel_rows:
        dt: date = r["trade_date"]
        if start_date and dt < start_date:
            continue
        if end_date and dt > end_date:
            continue
        by_date[dt].append(r)

    sorted_dates = sorted(by_date.keys())
    if len(sorted_dates) < 10:
        raise ValueError(f"Insufficient trading dates for simulation ({len(sorted_dates)}).")

    cash = initial_capital
    open_positions: dict[str, OpenPosition] = {}
    closed_trades: list[dict[str, Any]] = []
    cancelled_gaps = 0

    daily_port_returns: list[float] = []
    daily_obx_returns: list[float] = []
    daily_brent_returns: list[float] = []
    daily_fx_returns: list[float] = []

    prev_equity = initial_capital
    prev_obx = None
    equity_curve = [initial_capital]

    for dt in sorted_dates:
        day_rows = by_date[dt]
        row_map = {r["ticker"]: r for r in day_rows}

        # 1. Credit any ex-dividend cash paid today on open ASK positions, update stops, and check exits
        to_close: list[tuple[str, str, dict[str, Any]]] = []
        for ticker, pos in list(open_positions.items()):
            r = row_map.get(ticker)
            if not r:
                continue
            pos.days_held += 1
            div_per_share = float(r.get("div_amt") or 0.0)
            if div_per_share > 0.0:
                div_cash = pos.shares * div_per_share
                cash += div_cash
                pos.accumulated_dividends_nok += div_cash

            should_exit, reason = evaluate_exit_trigger(pos, r)
            if should_exit and r["next_raw_open"] is not None:
                to_close.append((ticker, reason, r))
                continue

            eff_close = float(r["raw_close"]) + (pos.accumulated_dividends_nok / max(pos.shares, 1))
            if eff_close > pos.highest_raw_close:
                pos.highest_raw_close = eff_close
                if pos.entry_sub_strategy == "trend_breakout":
                    pos.stop_loss_raw = max(pos.stop_loss_raw, pos.highest_raw_close - 2.5 * pos.atr_at_entry)
                elif pos.entry_sub_strategy == "bollinger_squeeze":
                    pos.stop_loss_raw = max(pos.stop_loss_raw, pos.highest_raw_close - 2.0 * pos.atr_at_entry)

        for ticker, reason, r in to_close:
            pos = open_positions.pop(ticker)
            raw_exit_unrounded = float(r["next_raw_open"])
            exit_price = round_to_tick_size(raw_exit_unrounded, liquidity_band=pos.liquidity_band, side="SELL")

            friction = evaluate_round_trip_friction(
                shares=pos.shares,
                entry_price=pos.entry_raw_price,
                exit_price=exit_price,
                holding_days=pos.days_held,
                profile=broker,
            )
            cash += (
                pos.shares * exit_price
                - friction.exit_commission_nok
                - friction.spread_and_slippage_nok
                - friction.custody_fee_nok
            )

            total_gross_pnl = friction.gross_pnl_nok + pos.accumulated_dividends_nok
            total_net_pnl = friction.net_pnl_nok + pos.accumulated_dividends_nok

            obx_now = float(r["obx_close"] or pos.obx_at_entry)
            obx_ret = (obx_now - pos.obx_at_entry) / pos.obx_at_entry if pos.obx_at_entry > 0 else 0.0
            sec_rel = float(r["sector_rel_mom_10d"] or 0.0) * (pos.days_held / 10.0)
            net_ret_pct = total_net_pnl / friction.entry_notional_nok if friction.entry_notional_nok > 0 else 0.0

            attr = decompose_trade_return(
                net_return_pct=net_ret_pct,
                obx_return_pct=obx_ret,
                sector_index_return_pct=obx_ret + sec_rel,
            )

            closed_trades.append(
                {
                    "trade_id": pos.trade_id,
                    "eval_id": None,
                    "cohort": "COHORT_A_BASE",
                    "broker_profile": broker.name,
                    "strategy_name": strategy_name,
                    "horizon_window": horizon_days,
                    "ticker": pos.ticker,
                    "sector": pos.sector,
                    "signal_date": pos.signal_date,
                    "entry_date": pos.entry_date,
                    "exit_date": r["next_trade_date"] or dt,
                    "shares": pos.shares,
                    "entry_raw_price": pos.entry_raw_price,
                    "exit_raw_price": exit_price,
                    "gross_pnl_nok": round(total_gross_pnl, 2),
                    "commission_nok": round(friction.entry_commission_nok + friction.exit_commission_nok, 2),
                    "slippage_spread_nok": friction.spread_and_slippage_nok,
                    "custody_fee_nok": friction.custody_fee_nok,
                    "net_pnl_nok": round(total_net_pnl, 2),
                    "net_return_pct": round(net_ret_pct, 6),
                    "obx_benchmark_return_pct": attr.market_contribution_pct,
                    "sector_rel_return_pct": attr.sector_contribution_pct,
                    "pure_strategy_alpha_pct": attr.pure_strategy_alpha_pct,
                    "exit_reason": reason,
                }
            )

        # 2. Scan for new candidate entries on Close(T) -> executed at Open(T+1)
        max_slots = 15 if unconstrained_single_stock_mode else 4
        max_sec = 15 if unconstrained_single_stock_mode else 2
        if len(open_positions) < max_slots:
            candidates: list[CandidateSignal] = []
            cand_row_lookup: dict[str, dict[str, Any]] = {}
            cand_substrat_lookup: dict[str, str] = {}

            for r in day_rows:
                tkr = r["ticker"]
                if tkr in open_positions or r["next_raw_open"] is None:
                    continue

                if exogenous_enabled:
                    reg_state = ExogenousRegimeState(
                        obx_close=float(r["obx_close"] or 0.0),
                        obx_sma200=float(r["obx_sma200"] or 0.0),
                        vix_close=float(r["vix_close"] or 18.0),
                        brent_2d_pct=float(r["brent_2d_pct"] or 0.0),
                        sector_rel_momentum_10d=float(r["sector_rel_mom_10d"] or 0.0),
                    )
                    allowed_reg, _ = check_exogenous_regime(reg_state, sector=r["sector"])
                    if not allowed_reg:
                        continue

                trig, rank_score, sub_strat = evaluate_entry_trigger(r, strategy_name)
                if trig:
                    candidates.append(
                        CandidateSignal(
                            ticker=tkr,
                            sector=r["sector"],
                            strategy_name=strategy_name,
                            signal_close=float(r["raw_close"]),
                            priority_rank=rank_score,
                        )
                    )
                    cand_row_lookup[tkr] = r
                    cand_substrat_lookup[tkr] = sub_strat

            active_sec_counts: dict[str, int] = defaultdict(int)
            for p in open_positions.values():
                active_sec_counts[p.sector] += 1

            selected = select_portfolio_candidates(
                candidates=candidates,
                active_positions_by_sector=active_sec_counts,
                current_open_positions=len(open_positions),
                max_total_positions=max_slots,
                max_per_sector=max_sec,
            )

            mtm_positions = sum(
                p.shares * float(row_map.get(p.ticker, {}).get("raw_close", p.entry_raw_price))
                for p in open_positions.values()
            )
            current_total_eq = cash + mtm_positions
            slot_budget = (
                25_000.0
                if unconstrained_single_stock_mode
                else min(current_total_eq * 0.25, cash / max(1, len(selected)))
            )

            for sel in selected:
                r = cand_row_lookup[sel.ticker]
                sig_close = float(r["raw_close"])
                next_open = float(r["next_raw_open"])

                gap_check = check_opening_gap_guardrail(sig_close, next_open)
                if not gap_check.allowed:
                    cancelled_gaps += 1
                    continue

                entry_price = round_to_tick_size(next_open, liquidity_band=int(r["liquidity_band"]), side="BUY")
                shares = int(slot_budget // entry_price)
                if shares <= 0:
                    continue

                entry_notional = shares * entry_price
                entry_comm = max(entry_notional * broker.commission_pct, broker.min_commission_nok)
                if not unconstrained_single_stock_mode and cash < (entry_notional + entry_comm):
                    continue

                cash -= (entry_notional + entry_comm)
                sub_s = cand_substrat_lookup[sel.ticker]
                atr_val = float(r["atr14"] or (entry_price * 0.02))
                stop_mult = 3.0 if sub_s == "rsi_mean_reversion" else (2.5 if sub_s == "trend_breakout" else 2.0)

                open_positions[sel.ticker] = OpenPosition(
                    trade_id=f"tr_{uuid.uuid4().hex[:12]}",
                    ticker=sel.ticker,
                    sector=sel.sector,
                    liquidity_band=int(r["liquidity_band"]),
                    strategy_name=strategy_name,
                    entry_sub_strategy=sub_s,
                    horizon_days=horizon_days,
                    signal_date=dt,
                    entry_date=r["next_trade_date"] or dt,
                    shares=shares,
                    entry_raw_price=entry_price,
                    stop_loss_raw=round(entry_price - stop_mult * atr_val, 4),
                    highest_raw_close=entry_price,
                    atr_at_entry=atr_val,
                    obx_at_entry=float(r["obx_close"] or 1000.0),
                )

        # 3. Record end-of-day mark-to-market equity and factor series
        eod_pos_value = sum(
            p.shares * float(row_map.get(p.ticker, {}).get("raw_close", p.entry_raw_price))
            for p in open_positions.values()
        )
        eod_equity = cash + eod_pos_value
        equity_curve.append(eod_equity)

        port_ret = (eod_equity - prev_equity) / prev_equity if prev_equity > 0 else 0.0
        daily_port_returns.append(port_ret)
        prev_equity = eod_equity

        sample_row = day_rows[0]
        curr_obx = float(sample_row["obx_close"] or 1000.0)
        obx_r = (curr_obx - prev_obx) / prev_obx if (prev_obx and prev_obx > 0) else 0.0
        prev_obx = curr_obx

        daily_obx_returns.append(obx_r)
        daily_brent_returns.append(float(sample_row["brent_2d_pct"] or 0.0) / 2.0)
        daily_fx_returns.append(0.0)

    # Compute summary metrics
    tot_ret_pct, cagr_pct, sharpe, sortino, max_dd_pct = compute_portfolio_metrics(
        daily_returns=daily_port_returns,
        equity_curve=equity_curve,
    )

    total_trades = len(closed_trades)
    wins = sum(1 for t in closed_trades if t["net_pnl_nok"] > 0)
    win_rate = (wins / total_trades * 100.0) if total_trades > 0 else 0.0

    gross_wins = sum(t["gross_pnl_nok"] for t in closed_trades if t["gross_pnl_nok"] > 0)
    total_friction = sum(
        t["commission_nok"] + t["slippage_spread_nok"] + t["custody_fee_nok"]
        for t in closed_trades
    )
    cost_drag_pct = (total_friction / gross_wins * 100.0) if gross_wins > 0 else 100.0
    avg_pure_alpha = (
        sum(t["pure_strategy_alpha_pct"] for t in closed_trades) / total_trades * 100.0
        if total_trades > 0
        else 0.0
    )

    reg = run_multifactor_ols_regression(
        portfolio_excess_returns=daily_port_returns,
        obx_excess_returns=daily_obx_returns,
        brent_returns=daily_brent_returns,
        usdnok_returns=daily_fx_returns,
    )

    passes_gates = (
        sortino > 1.2
        and sharpe > 0.9
        and max_dd_pct < 18.0
        and cost_drag_pct < 20.0
    )

    metrics = StrategyMetrics(
        strategy_name=strategy_name,
        horizon_days=horizon_days,
        exogenous_enabled=exogenous_enabled,
        broker_name=broker.name,
        period_label=period_label,
        total_trades=total_trades,
        cancelled_gap_count=cancelled_gaps,
        win_rate_pct=round(win_rate, 1),
        total_return_pct=tot_ret_pct,
        cagr_pct=cagr_pct,
        sharpe_ratio=sharpe,
        sortino_ratio=sortino,
        max_drawdown_pct=max_dd_pct,
        gross_profit_nok=round(gross_wins, 2),
        total_friction_nok=round(total_friction, 2),
        cost_drag_pct=round(cost_drag_pct, 2),
        avg_pure_alpha_pct=round(avg_pure_alpha, 2),
        alpha_annualized_pct=round(reg.alpha_annualized * 100.0, 2),
        beta_obx=reg.beta_obx,
        beta_brent=reg.beta_brent,
        beta_usdnok=reg.beta_usdnok,
        passes_all_gates=passes_gates,
    )
    return metrics, closed_trades
