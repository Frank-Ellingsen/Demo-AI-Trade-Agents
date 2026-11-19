"""Unit tests for the 5-agent OBX swing-trading architecture."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from datetime import date

from improved_execution import (
    LiquidityBand,
    OrderSide,
    build_saxo_order_payload,
    compute_endogenous_signals,
    get_mifid2_tick_size,
    round_to_valid_tick,
    validate_execution_guardrails,
)
from intuitive_backtest_engine import generate_intuitive_comparison
from src.analytics.attribution import decompose_trade_return, run_multifactor_ols_regression
from src.brokers.saxo_execution import SaxoRateLimiter, build_saxo_order_request
from src.db.duckdb_store import init_duckdb
from src.db.sqlite_store import init_sqlite_db
from src.hitl.llm_validator import evaluate_signal_with_risk_officer
from src.hitl.order_queue import enqueue_order_proposal, expire_stale_orders, update_order_status
from src.hitl.telegram_bot import format_buy_proposal_message, process_telegram_callback
from src.portfolio.allocator import CandidateSignal, select_portfolio_candidates
from src.portfolio.cost_tax_model import (
    NORDNET_MINI,
    SAXO_CLASSIC,
    evaluate_round_trip_friction,
    round_to_tick_size,
)
from src.portfolio.risk_manager import check_opening_gap_guardrail
from src.signals.exogenous import ExogenousRegimeState, check_exogenous_regime


def test_saxo_vs_nordnet_cost_drag_gate() -> None:
    """Verify Saxo vs. Nordnet round-trip fees and the 20% Cost Drag ceiling on a 25,000 NOK position."""
    # 100 shares bought at 250 NOK (25,000 NOK) and sold at 255 NOK (+2.0% / +500 NOK gross profit)
    saxo_res = evaluate_round_trip_friction(
        shares=100,
        entry_price=250.0,
        exit_price=255.0,
        holding_days=10,
        profile=SAXO_CLASSIC,
    )
    nordnet_res = evaluate_round_trip_friction(
        shares=100,
        entry_price=250.0,
        exit_price=255.0,
        holding_days=10,
        profile=NORDNET_MINI,
    )
    assert saxo_res.gross_pnl_nok == 500.0
    assert saxo_res.entry_commission_nok == 20.0
    assert saxo_res.passes_cost_drag_gate is True
    assert nordnet_res.entry_commission_nok == 37.50
    assert nordnet_res.total_cost_nok > saxo_res.total_cost_nok


def test_euronext_mifid_tick_size_rounding() -> None:
    """Verify Band 5 and Band 6 tick-size rounding on Oslo Børs (MiFID II RTS 11)."""
    # Band 6 (EQNR.OL at 285.17 NOK in [200, 500) -> tick size 0.05)
    assert round_to_tick_size(285.17, liquidity_band=6, side="BUY") == 285.15
    assert round_to_tick_size(285.17, liquidity_band=6, side="SELL") == 285.20
    # Band 5 (AKRBP.OL at 245.35 NOK in [200, 500) -> tick size 0.10)
    assert round_to_tick_size(245.35, liquidity_band=5, side="BUY") == 245.30
    assert round_to_tick_size(245.35, liquidity_band=5, side="SELL") == 245.40


def test_opening_gap_guardrail() -> None:
    """Verify 09:00 CET opening gap protection (+1.5% / -2.0%)."""
    assert check_opening_gap_guardrail(100.0, 101.2).allowed is True
    assert check_opening_gap_guardrail(100.0, 102.1).allowed is False
    assert check_opening_gap_guardrail(100.0, 97.5).allowed is False


def test_portfolio_sector_cap_and_tie_breaker() -> None:
    """Verify max 4 positions and max 2 per sector with deterministic priority_rank tie-breaking."""
    candidates = [
        CandidateSignal("EQNR.OL", "Energy", "rsi_mean_reversion", 280.0, priority_rank=94.0),
        CandidateSignal("AKRBP.OL", "Energy", "rsi_mean_reversion", 240.0, priority_rank=92.0),
        CandidateSignal("SUBC.OL", "Energy", "rsi_mean_reversion", 190.0, priority_rank=96.0),
        CandidateSignal("DNB.OL", "Financials", "rsi_mean_reversion", 215.0, priority_rank=88.0),
    ]
    selected = select_portfolio_candidates(
        candidates=candidates,
        active_positions_by_sector={},
        current_open_positions=0,
        max_total_positions=4,
        max_per_sector=2,
    )
    selected_tickers = [c.ticker for c in selected]
    # SUBC (96) and EQNR (94) take the 2 Energy slots; AKRBP (92) is capped out; DNB takes slot 3
    assert selected_tickers == ["SUBC.OL", "EQNR.OL", "DNB.OL"]


def test_exogenous_energy_vix_override() -> None:
    """Verify that Energy/Shipping stocks can pass VIX >= 25 when Brent 2d change >= +1.5%."""
    high_vix_oil_spike = ExogenousRegimeState(
        obx_close=1350.0,
        obx_sma200=1280.0,
        vix_close=28.5,
        brent_2d_pct=0.024,
        sector_rel_momentum_10d=0.012,
    )
    allowed_energy, _ = check_exogenous_regime(high_vix_oil_spike, sector="Energy")
    allowed_finance, _ = check_exogenous_regime(high_vix_oil_spike, sector="Financials")
    assert allowed_energy is True
    assert allowed_finance is False


def test_sqlite_wal_hitl_queue_and_14h_expiry(tmp_path: Path) -> None:
    """Verify Agent 4 SQLite WAL queue: BUY requires HITL, SELL is auto-approved, 14h expiry works."""
    db_file = tmp_path / "test_hitl.db"
    init_sqlite_db(db_file)

    buy_order = enqueue_order_proposal(
        ticker="EQNR.OL",
        action="BUY",
        shares=85,
        signal_close_price=285.0,
        limit_price=285.0,
        stop_loss_price=272.0,
        strategy_name="rsi_mean_reversion",
        expiry_hours=14,
        db_path=db_file,
    )
    sell_order = enqueue_order_proposal(
        ticker="EQNR.OL",
        action="SELL",
        shares=85,
        signal_close_price=292.0,
        limit_price=292.0,
        stop_loss_price=None,
        strategy_name="rsi_mean_reversion",
        db_path=db_file,
    )
    assert buy_order["status"] == "PENDING"
    assert buy_order["requires_hitl"] is True
    assert sell_order["status"] == "AUTO_APPROVED"
    assert sell_order["requires_hitl"] is False

    # Advance clock by 15 hours to trigger 14h auto-expiry
    future_time = datetime.now(timezone.utc) + timedelta(hours=15)
    expired_count = expire_stale_orders(reference_time=future_time, db_path=db_file)
    assert expired_count == 1


def test_agent3_earnings_guardrail_and_duckdb_logging(tmp_path: Path) -> None:
    """Verify Agent 3 blocks trades < 3 days before quarterly earnings and logs to DuckDB."""
    duck_file = tmp_path / "test_analytics.duckdb"
    init_duckdb(duck_file)

    card = evaluate_signal_with_risk_officer(
        ticker="NOD.OL",
        strategy_name="trend_breakout",
        horizon_days=20,
        signal_close_price=142.5,
        atr_stop_loss=131.0,
        news_context_72h="Q3-presentasjon publiseres om 2 dager kl 07:00 CET.",
        days_until_earnings=2,
        duckdb_path=duck_file,
    )
    assert card.approve_recommendation is False
    assert card.earnings_blackout_flag is True
    assert card.rejection_category == "EARNINGS_SOON"
    assert card.risk_score == "HIGH"


def test_expired_approval_is_rejected_and_downside_gap_is_blocked(tmp_path: Path) -> None:
    db_file = tmp_path / "expiry.db"
    init_sqlite_db(db_file)
    queued = enqueue_order_proposal(
        ticker="EQNR.OL",
        action="BUY",
        shares=10,
        signal_close_price=284.0,
        limit_price=284.0,
        stop_loss_price=270.0,
        strategy_name="rsi_mean_reversion",
        expiry_hours=0,
        db_path=db_file,
    )

    assert update_order_status(queued["order_id"], "APPROVED", db_path=db_file) is False

    validation = validate_execution_guardrails(
        signal_close_price=284.0,
        current_market_price=278.0,
        side=OrderSide.BUY,
        max_slippage_pct=1.5,
        band=LiquidityBand.BAND_6,
    )
    assert validation.is_valid is False
    assert validation.status == "REJECTED_PRICE_GAPPED_DOWN"


def test_agent5_return_attribution() -> None:
    """Verify Agent 5 factor decomposition (Market + Sector + Pure Strategy Alpha)."""
    attr = decompose_trade_return(
        net_return_pct=0.045,        # +4.5% net trade return
        obx_return_pct=0.012,        # +1.2% OBX return
        sector_index_return_pct=0.020,  # +2.0% Sector return (+0.8% relative to OBX)
    )
    assert attr.market_contribution_pct == 0.012
    assert attr.sector_contribution_pct == 0.008
    assert attr.pure_strategy_alpha_pct == 0.025


def test_agent5_multifactor_ols_regression() -> None:
    """Verify Agent 5 OLS factor regression (alpha, beta_OBX, beta_Brent, beta_USDNOK)."""
    obx = [0.01, -0.005, 0.008, -0.012, 0.015, 0.003, -0.004, 0.009]
    brent = [0.02, -0.01, 0.005, -0.02, 0.01, -0.005, 0.015, 0.004]
    fx = [-0.002, 0.004, -0.001, 0.006, -0.003, 0.001, 0.002, -0.004]
    # True data-generating process: alpha = 0.0005/day, beta_OBX = 0.8, beta_Brent = 0.3, beta_FX = -0.2
    port = [
        0.0005 + 0.8 * obx[i] + 0.3 * brent[i] - 0.2 * fx[i]
        for i in range(len(obx))
    ]
    res = run_multifactor_ols_regression(port, obx, brent, fx)
    assert res.alpha_daily == 0.0005
    assert res.beta_obx == 0.8
    assert res.beta_brent == 0.3
    assert res.beta_usdnok == -0.2
    assert res.r_squared >= 0.9999


def test_agent4_telegram_callback_and_saxo_idempotency(tmp_path: Path) -> None:
    """Verify Telegram inline button callback updates SQLite WAL and Saxo builder sets X-Request-ID."""
    db_file = tmp_path / "test_tg.db"
    init_sqlite_db(db_file)

    queued = enqueue_order_proposal(
        ticker="EQNR.OL",
        action="BUY",
        shares=85,
        signal_close_price=285.0,
        limit_price=285.0,
        stop_loss_price=272.0,
        strategy_name="rsi_mean_reversion",
        db_path=db_file,
    )
    msg_text, markup = format_buy_proposal_message(
        order_id=queued["order_id"],
        ticker="EQNR.OL",
        strategy_name="rsi_mean_reversion",
        shares=85,
        limit_price=285.0,
        stop_loss_price=272.0,
        llm_recommendation="GO",
        risk_score="LOW",
        bull_case="Oversolgt i primær opptrend",
        devils_advocate="Oljeprisfall",
        expires_at_iso=queued["expires_at"],
    )
    assert "EQNR.OL" in msg_text
    assert markup["inline_keyboard"][0][0]["callback_data"] == f"APPROVE:{queued['order_id']}"

    oid, new_status = process_telegram_callback(f"APPROVE:{queued['order_id']}", db_path=db_file)
    assert oid == queued["order_id"]
    assert new_status == "APPROVED"

    saxo_req = build_saxo_order_request(
        uic=2115,
        action="BUY",
        shares=85,
        signal_close_price=285.0,
        live_open_price=286.17,
        liquidity_band=6,
        x_request_id=queued["x_request_id"],
    )
    assert saxo_req.headers["X-Request-ID"] == queued["x_request_id"]
    assert saxo_req.body["OrderPrice"] == 286.15  # Band 6 tick size 0.05 rounded down for BUY

    # Rate limiter check
    clock = [100.0]
    slept = []
    limiter = SaxoRateLimiter(min_order_interval_sec=1.0)
    limiter.wait_for_order_slot(now_fn=lambda: clock[0], sleep_fn=lambda s: slept.append(s))
    clock[0] = 100.4
    waited = limiter.wait_for_order_slot(now_fn=lambda: clock[0], sleep_fn=lambda s: slept.append(s))
    assert waited == 0.6


def test_improved_execution_mifid2_boundary_and_guardrails() -> None:
    """Verify full MiFID II table (Bands 1-6), boundary tick transitions, and PreExecutionValidation."""
    import pandas as pd

    # Boundary transition check: buying at 100.01 in Band 5 (tick=0.05 above 100, 0.02 below 100)
    assert get_mifid2_tick_size(99.5, LiquidityBand.BAND_5) == 0.02
    assert get_mifid2_tick_size(100.5, LiquidityBand.BAND_5) == 0.05
    assert round_to_valid_tick(285.17, OrderSide.BUY, LiquidityBand.BAND_6) == 285.15

    # Pre-execution guardrail approval and rejection
    ok_val = validate_execution_guardrails(100.0, 101.2, OrderSide.BUY, max_slippage_pct=1.5, band=LiquidityBand.BAND_5)
    assert ok_val.is_valid is True
    assert ok_val.status == "APPROVED_FOR_EXECUTION"

    bad_val = validate_execution_guardrails(100.0, 102.0, OrderSide.BUY, max_slippage_pct=1.5, band=LiquidityBand.BAND_5)
    assert bad_val.is_valid is False
    assert bad_val.status == "REJECTED_PRICE_GAPPED_UP"

    # Dual-price DataFrame signal calculation
    df = pd.DataFrame(
        {
            "adj_close": [100.0 + i * 0.2 for i in range(205)],
            "raw_close": [95.0 + i * 0.2 for i in range(205)],
            "volume": [100_000 for _ in range(205)],
        }
    )
    out = compute_endogenous_signals(df)
    assert "signal_buy" in out.columns
    assert "exec_limit_price" in out.columns


def test_intuitive_backtest_engine_comparison() -> None:
    """Verify `intuitive_backtest_engine.py` generates all 6 OBX 15 comparison cases."""
    df = generate_intuitive_comparison()
    assert len(df) == 6
    assert "Case ID" in df.columns
    assert "Sortino" in df.columns
    assert "Cost Drag" in df.columns


def test_agent3_offline_fail_closed(monkeypatch) -> None:
    import src.hitl.llm_validator as validator

    monkeypatch.setattr(validator, "_call_ollama_json", lambda **kwargs: {})
    monkeypatch.setattr(validator, "_call_gemini_json", lambda **kwargs: {})

    card = validator.evaluate_signal_with_risk_officer(
        ticker="EQNR.OL",
        strategy_name="rsi_mean_reversion",
        horizon_days=20,
        signal_close_price=285.0,
        atr_stop_loss=272.0,
        news_context_72h="Ingen hendelser.",
        days_until_earnings=None,
    )
    assert card.approve_recommendation is False
    assert card.rejection_category == "COMPANY_SHOCK"
    assert card.llm_provider == "offline_fail_closed"


def test_agent3_uses_actual_signal_date(tmp_path: Path) -> None:
    import duckdb
    import src.hitl.llm_validator as validator

    duck_file = tmp_path / "signal_date.duckdb"
    init_duckdb(duck_file)
    card = validator.evaluate_signal_with_risk_officer(
        ticker="EQNR.OL",
        strategy_name="rsi_mean_reversion",
        horizon_days=20,
        signal_close_price=285.0,
        atr_stop_loss=272.0,
        news_context_72h="Ingen hendelser.",
        days_until_earnings=None,
        duckdb_path=duck_file,
        signal_date=date(2026, 11, 17),
    )
    con = duckdb.connect(str(duck_file), read_only=True)
    try:
        logged_date = con.execute(
            "SELECT signal_date FROM signal_evaluations WHERE eval_id = ?",
            [card.eval_id],
        ).fetchone()[0]
    finally:
        con.close()
    assert str(logged_date) == "2026-11-17"


def test_allocator_respects_available_cash() -> None:
    candidates = [
        CandidateSignal("EQNR.OL", "Energy", "trend_breakout", 280.0, 99.0),
        CandidateSignal("DNB.OL", "Financials", "trend_breakout", 215.0, 98.0),
    ]
    selected = select_portfolio_candidates(
        candidates=candidates,
        active_positions_by_sector={},
        current_open_positions=0,
        max_total_positions=4,
        max_per_sector=2,
        available_cash_nok=30_000.0,
        position_notional_nok=25_000.0,
    )
    assert [c.ticker for c in selected] == ["EQNR.OL"]
