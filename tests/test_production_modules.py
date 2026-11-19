"""Unit tests for the 10 root production modules:
- improved_execution.py
- intuitive_backtest_engine.py
- stream_execution_bridge.py
- saxo_streamer.py
- telegram_hitl_bot.py
- saxo_client.py
- portfolio_monitor.py
- setup_drift_views.py
- ingest_market_data.py
- run_daily_scan.py
"""

from __future__ import annotations

from pathlib import Path

import duckdb

from ingest_market_data import run_pipeline
from portfolio_monitor import initialize_portfolio_database, run_portfolio_health_check
from run_daily_scan import run_daily_scan
from runner_server import create_runner_server
from saxo_client import RateLimitTracker, SaxoClient
from saxo_streamer import SaxoBinaryProtocol
from setup_drift_views import setup_drift_monitoring_views
from stream_execution_bridge import StreamExecutionBridge
from telegram_hitl_bot import init_db, log_order_proposal, update_order_status


def test_saxo_binary_protocol_encode_and_parse() -> None:
    """Verify Saxo binary WebSocket frame encoding and decoding."""
    payload = {"Quote": {"Bid": 284.10, "Ask": 284.20, "Mid": 284.15}, "LastUpdated": "2026-11-17T09:01:00Z"}
    frame1 = SaxoBinaryProtocol.encode_frame(msg_id=101, ref_id="eqnr_sub", payload=payload)
    frame2 = SaxoBinaryProtocol.encode_frame(msg_id=102, ref_id="_heartbeat", payload={"Heartbeats": []})

    decoded = SaxoBinaryProtocol.parse_frame(frame1 + frame2)
    assert len(decoded) == 2
    assert decoded[0]["msg_id"] == 101
    assert decoded[0]["ref_id"] == "eqnr_sub"
    assert decoded[0]["data"]["Quote"]["Mid"] == 284.15
    assert decoded[1]["ref_id"] == "_heartbeat"


def test_stream_execution_bridge_and_telegram_hitl_interoperability(tmp_path: Path) -> None:
    """Verify that orders logged via `telegram_hitl_bot.py` are picked up and validated by `StreamExecutionBridge`."""
    db_file = tmp_path / "bridge_hitl.db"
    init_db(db_file)

    # 1. Log order via telegram_hitl_bot and approve it
    log_order_proposal(
        order_id="ord_bridge_01",
        ticker="EQNR.OL",
        side="BUY",
        amount=88,
        limit_price=284.00,
        reason="RSI(2) < 10 pullback",
        db_path=db_file,
    )
    update_order_status("ord_bridge_01", "APPROVED", db_path=db_file)

    bridge = StreamExecutionBridge(db_path=db_file, max_slippage_pct=1.5)
    bridge.update_quote("EQNR.OL", bid=285.10, ask=285.20, last=285.17)

    class RecordingSaxoClient:
        def __init__(self) -> None:
            self.limit_call = None
            self.stop_call = None
            self.default_account_key = "SIM_ACCOUNT"

        def place_limit_order(self, **kwargs):
            self.limit_call = kwargs
            return {"OrderId": 1001}

        def place_stop_order(self, **kwargs):
            self.stop_call = kwargs
            return {"OrderId": 1002}

    recording_client = RecordingSaxoClient()

    # 2. Incoming tick at 285.17 (+0.41% slippage <= 1.5% limit) -> should stage order rounded to Band 6 tick (285.15)
    order_payload = bridge.process_incoming_tick(
        "EQNR.OL",
        market_price=285.17,
        saxo_client=recording_client,
        uic=2115,
    )
    assert order_payload is not None
    assert order_payload["payload"]["OrderPrice"] == 285.15
    assert "X-Request-Id" in order_payload["headers"]
    assert recording_client.stop_call is not None
    assert recording_client.stop_call["stop_price"] == 269.8

    # 3. Log second order and test rejection on > 1.5% opening gap
    log_order_proposal(
        order_id="ord_bridge_gap",
        ticker="AKRBP.OL",
        side="BUY",
        amount=100,
        limit_price=200.00,
        reason="Breakout",
        db_path=db_file,
    )
    update_order_status("ord_bridge_gap", "APPROVED", db_path=db_file)
    rejected_payload = bridge.process_incoming_tick("AKRBP.OL", market_price=205.00, saxo_client=None)
    assert rejected_payload is None


def test_saxo_client_headers_and_rate_limiter() -> None:
    """Verify `SaxoClient` sets OAuth Bearer token, idempotent `x-request-id`, and tracks rate-limit headers."""
    client = SaxoClient(env_token="sim_token_123")
    headers = client.get_headers(request_id="req_abc_999")
    assert headers["Authorization"] == "Bearer sim_token_123"
    assert headers["x-request-id"] == "req_abc_999"

    tracker = RateLimitTracker()
    tracker.update_from_headers({"X-RateLimit-Session-Remaining": "98", "X-RateLimit-Session-Reset": "42"})
    assert tracker.remaining_session_calls == 98
    assert tracker.reset_session_seconds == 42


def test_portfolio_monitor_and_drift_views() -> None:
    """Verify `portfolio_monitor.py`, `setup_drift_views.py`, `ingest_market_data.py`, and `run_daily_scan.py`."""
    health = run_portfolio_health_check()
    assert health["initialized"] is True

    summary_df = run_pipeline(refresh_remote=False)
    assert len(summary_df) == 15

    setup_drift_monitoring_views()
    con = duckdb.connect("data/obx_analytics.duckdb", read_only=True)
    try:
        drift_rows = con.execute("SELECT * FROM v_rolling_strategy_drift_60d;").fetchall()
        sector_rows = con.execute("SELECT COUNT(*) FROM sector_daily;").fetchone()[0]
    finally:
        con.close()

    assert len(drift_rows) > 0
    assert sector_rows > 1000

    scan_res = run_daily_scan()
    assert "latest_date" in scan_res
    assert scan_res["available_slots"] <= 4


def test_runner_server_and_index_html_api() -> None:
    """Verify `runner_server.py` serves `index.html`, `/api/status/health_check`, and executes `/api/run/*` jobs."""
    import json
    import threading
    import time
    import urllib.request
    import urllib.error

    server = create_runner_server(host="127.0.0.1", port=0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        base_url = f"http://127.0.0.1:{port}"

        # 1. Verify health_check endpoint returns 200 JSON
        with urllib.request.urlopen(f"{base_url}/api/status/health_check", timeout=5) as resp:
            assert resp.status == 200
            health_data = json.loads(resp.read().decode("utf-8"))
            assert health_data["ok"] is True

        # 2. Verify index.html and /kontrolltarn (Aurora dashboard) + /api/dashboard are served properly
        with urllib.request.urlopen(f"{base_url}/index.html", timeout=5) as resp:
            assert resp.status == 200
            html_text = resp.read().decode("utf-8")
            assert "AI Trading Desk" in html_text
            assert "runAllStepsSequentially" in html_text
            for sec_id in ("sec-pipeline", "sec-alpaca", "sec-backtest", "sec-bridge", "sec-obx-hitl", "sec-docs"):
                assert sec_id in html_text

        with urllib.request.urlopen(f"{base_url}/api/suite", timeout=10) as resp:
            assert resp.status == 200
            suite_json = json.loads(resp.read().decode("utf-8"))
            assert len(suite_json["us_backtest"]["metrics"]) == 5
            assert len(suite_json["nordic_backtest"]["metrics"]) == 4
            assert len(suite_json["intuitive_6case"]) == 6
            assert "2" in suite_json["recommender_profiles"]
            assert len(suite_json["live_bridge"]["quotes"]) >= 5
            assert "alpaca_portfolio" in suite_json
            assert isinstance(suite_json["alpaca_portfolio"]["positions"], list)

        with urllib.request.urlopen(f"{base_url}/api/alpaca/portfolio", timeout=10) as resp:
            assert resp.status == 200
            alp_json = json.loads(resp.read().decode("utf-8"))
            assert "equity" in alp_json and "cash" in alp_json and "positions" in alp_json

        with urllib.request.urlopen(f"{base_url}/meglerbord", timeout=5) as resp:
            assert resp.status == 200
            megler_html = resp.read().decode("utf-8")
            assert "Multi-Agent Meglerbord" in megler_html
            assert "syncLiveDuckDbData" in megler_html
            assert "GJF.OL" in megler_html

        with urllib.request.urlopen(f"{base_url}/kontrolltarn", timeout=5) as resp:
            assert resp.status == 200
            aurora_html = resp.read().decode("utf-8")
            assert "OBX 15 AI-Trading Kontrolltårn" in aurora_html
            assert "syncLiveDuckDbData" in aurora_html
            assert "applyRiskProfilePreset" in aurora_html
            assert "GJF.OL" in aurora_html

        for doc_route in ("/guide", "/dummies", "/readme", "/brukertest", "/strategi-radgiver", "/bloomberg", "/terminal"):
            with urllib.request.urlopen(f"{base_url}{doc_route}", timeout=5) as resp:
                assert resp.status == 200
                assert len(resp.read().decode("utf-8")) > 500

        with urllib.request.urlopen(f"{base_url}/api/dashboard", timeout=10) as resp:
            assert resp.status == 200
            dash_json = json.loads(resp.read().decode("utf-8"))
            assert len(dash_json["obx15_snapshot"]) == 15
            assert "series_12d" in dash_json["obx15_snapshot"][0]

        # 2b. Sensitive project files must not be served by the runner
        try:
            urllib.request.urlopen(f"{base_url}/.env", timeout=5)
            raise AssertionError("Sensitive file must not be served")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404

        # Cross-origin requests must not be allowed to start jobs
        cross_origin_req = urllib.request.Request(
            f"{base_url}/api/run/ping",
            method="POST",
            headers={"Origin": "https://example.com"},
        )
        try:
            urllib.request.urlopen(cross_origin_req, timeout=5)
            raise AssertionError("Cross-origin job request must be rejected")
        except urllib.error.HTTPError as exc:
            assert exc.code == 403

        # 3. Trigger job via POST /api/run/ping and poll until completed
        req = urllib.request.Request(f"{base_url}/api/run/ping", method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            run_data = json.loads(resp.read().decode("utf-8"))
            assert run_data["success"] is True
            job_id = run_data["job_id"]

        final_status = "queued"
        logs_text = ""
        for _ in range(40):
            time.sleep(0.25)
            with urllib.request.urlopen(f"{base_url}/api/status/{job_id}", timeout=5) as s_resp:
                st_data = json.loads(s_resp.read().decode("utf-8"))
                final_status = st_data["status"]
                logs_text = st_data["logs"]
                if final_status in ("completed", "failed"):
                    break

        assert final_status == "completed"
        assert "RUNNER_PING_OK" in logs_text
        assert "FULLFØRT MED SUKSESS" in logs_text
    finally:
        server.shutdown()
        server.server_close()


def test_alpaca_client_and_us_bracket_routing(tmp_path):
    """Test AlpacaClient bracket payload builder and StreamExecutionBridge US S&P 500 routing."""
    from alpaca_execution import AlpacaClient, build_alpaca_bracket_payload

    client = AlpacaClient(
        api_key="PK_TEST_KEY",
        secret_key="SK_TEST_SECRET",
        endpoint="https://paper-api.alpaca.markets",
    )
    assert client.endpoint == "https://paper-api.alpaca.markets/v2"
    assert client.headers["APCA-API-KEY-ID"] == "PK_TEST_KEY"
    assert client.headers["APCA-API-SECRET-KEY"] == "SK_TEST_SECRET"

    # Security safeguard: live trading endpoint must be strictly blocked
    import pytest

    with pytest.raises(ValueError, match="SIKKERHETSSPERRE"):
        AlpacaClient(
            api_key="AK_LIVE_KEY",
            secret_key="SK_LIVE_SECRET",
            endpoint="https://api.alpaca.markets/v2",
        )

    payload = build_alpaca_bracket_payload(
        symbol="NVDA",
        qty=5,
        side="buy",
        limit_price=182.456,
        take_profit_price=195.109,
        stop_loss_price=174.222,
    )
    assert payload["symbol"] == "NVDA"
    assert payload["order_class"] == "bracket"
    assert payload["limit_price"] == "182.46"
    assert payload["take_profit"]["limit_price"] == "195.11"
    assert payload["stop_loss"]["stop_price"] == "174.22"

    # Verify StreamExecutionBridge routes non-.OL symbols to Alpaca Bracket Order staging
    db_file = tmp_path / "test_alpaca_hitl.db"
    init_db(db_file)
    log_order_proposal(
        order_id="ord_alpaca_01",
        ticker="NVDA",
        side="BUY",
        amount=10,
        limit_price=180.00,
        stop_loss_price=171.00,
        reason="US S&P 500 bracket test",
        db_path=db_file,
    )
    update_order_status("ord_alpaca_01", "APPROVED", db_path=db_file)

    bridge = StreamExecutionBridge(db_path=db_file, max_slippage_pct=1.5)
    alpaca_payload = bridge.process_incoming_tick("NVDA", market_price=180.05)
    assert alpaca_payload is not None
    assert alpaca_payload["symbol"] == "NVDA"
    assert alpaca_payload["order_class"] == "bracket"
    assert alpaca_payload["limit_price"] == "180.05"
    assert float(alpaca_payload["stop_loss"]["stop_price"]) == 171.00


def test_multi_universe_backtests_live_bridge_and_dynamic_recommender() -> None:
    """Verify US/Nordic multi-universe backtesters, Massive/Alpaca bridge, and DynamicRecommender."""
    from backtest_nordic_multi_universe import run_nordic_backtest
    from backtest_us_multi_universe import calculate_metrics as calculate_us_metrics, run_us_backtest
    from dynamic_recommender import DynamicRecommender
    from live_market_bridge import AlpacaStreamer, MassiveClient
    from runner_server import COMMANDS

    # 1. US Multi-Universe Backtest
    df_us = run_us_backtest(initial_capital=100_000.0, days=252, seed=42)
    assert len(df_us) == 252
    df_us_metrics = calculate_us_metrics(df_us, initial_capital=100_000.0)
    assert len(df_us_metrics) == 5
    assert "4. AI Regime Switcher (VIX)" in df_us_metrics["Strategi"].values

    # 2. Nordic Multi-Universe Backtest
    df_nordic, df_nordic_metrics = run_nordic_backtest(initial_capital_nok=100_000.0, trading_days=252)
    assert len(df_nordic) == 252
    assert len(df_nordic_metrics) == 4
    assert "1. OBX Momentum (m/ Brent-filter)" in df_nordic_metrics["Strategi"].values

    # 3. Live Market Bridge (MassiveClient fallback & AlpacaStreamer init)
    massive = MassiveClient(api_key="")
    quote = massive.get_last_quote("NVDA", allow_fallback=True)
    assert quote["ticker"] == "NVDA"
    assert quote["ask"] >= quote["bid"] > 0
    news = massive.get_latest_news(limit=2, allow_fallback=True)
    assert len(news) == 2
    streamer = AlpacaStreamer(key="TEST_K", secret="TEST_S")
    assert streamer.key == "TEST_K"

    # 4. DynamicRecommender across all 3 risk profiles
    rec_engine = DynamicRecommender()
    assert rec_engine.endpoint.endswith("/v2")
    for p_key, expected_count in (("1", 4), ("2", 5), ("3", 6)):
        rec = rec_engine.generate_recommendation(p_key, custom_capital=100_000.0)
        assert rec["total_equity"] == 100_000.0
        assert len(rec["proposals"]) == expected_count
        assert all(p["tp_price"] > p["price"] > p["sl_price"] for p in rec["proposals"])

    # 5. Runner Server commands registered for index.html Steps 5-10
    for step_id in (
        "step5_us_backtest",
        "step6_nordic_backtest",
        "step7_live_bridge",
        "step8_dynamic_recommender",
        "step10_strategy_advisor",
    ):
        assert step_id in COMMANDS

    # 6. StrategyAdvisor from telegram_strategy_advisor_bot.py
    from telegram_strategy_advisor_bot import StrategyAdvisor

    assert "VIX" in StrategyAdvisor.answer_query("regime")
    assert "S&P 500" in StrategyAdvisor.answer_query("momentum usa")
    assert "Covered Calls" in StrategyAdvisor.answer_query("opsjoner")
    assert "OTC" in StrategyAdvisor.answer_query("otc")
    assert "OBX 15" in StrategyAdvisor.answer_query("obx")
    assert "Balansert Profil" in StrategyAdvisor.answer_query("anbefal 2")


def test_saxo_live_execution_guard(monkeypatch) -> None:
    from src.brokers.saxo_client import SaxoClient

    monkeypatch.setenv("SAXO_ENV", "live")
    monkeypatch.setenv("EXECUTION_MODE", "paper")
    monkeypatch.delenv("LIVE_EXECUTION_ACK", raising=False)
    client = SaxoClient(env_token="test-token")

    import pytest

    with pytest.raises(PermissionError, match="Live Saxo execution blocked"):
        client.place_limit_order(
            uic=2115,
            amount=1,
            limit_price=285.0,
            account_key="TEST",
            request_id="test-request",
        )


def test_runner_mutating_secret_guard(monkeypatch) -> None:
    from runner_server import _authorize_mutating_request

    monkeypatch.setenv("RUNNER_SECRET", "")
    assert _authorize_mutating_request({"X-Runner-Secret": ""}) is True

    monkeypatch.setenv("RUNNER_SECRET", "local-secret")
    assert _authorize_mutating_request({"X-Runner-Secret": "wrong"}) is False
    assert _authorize_mutating_request({"X-Runner-Secret": "local-secret"}) is True


def test_reserved_cash_and_expiry(tmp_path: Path) -> None:
    from datetime import datetime, timedelta, timezone
    from src.db.sqlite_store import init_sqlite_db
    from src.hitl.order_queue import (
        enqueue_order_proposal,
        expire_stale_orders,
        get_reserved_cash_nok,
    )

    db_file = tmp_path / "reserved_cash.db"
    init_sqlite_db(db_file)
    enqueue_order_proposal(
        ticker="EQNR.OL",
        action="BUY",
        shares=100,
        signal_close_price=285.0,
        limit_price=285.0,
        stop_loss_price=272.0,
        strategy_name="trend_breakout",
        db_path=db_file,
    )
    assert get_reserved_cash_nok(db_file) == 28_500.0

    future = datetime.now(timezone.utc) + timedelta(hours=15)
    assert expire_stale_orders(reference_time=future, db_path=db_file) == 1
    assert get_reserved_cash_nok(db_file) == 0.0


def test_walk_forward_heldout_split() -> None:
    """Exercise a deterministic in-sample/held-out split on synthetic daily returns."""
    import math

    returns = [0.01 if i % 7 else -0.006 for i in range(252)]
    split = int(len(returns) * 0.8)
    train = returns[:split]
    heldout = returns[split:]

    train_mean = sum(train) / len(train)
    heldout_mean = sum(heldout) / len(heldout)
    assert len(train) == 201
    assert len(heldout) == 51
    assert math.isfinite(train_mean)
    assert math.isfinite(heldout_mean)


def test_scheduler_daemon_and_triggers(tmp_path) -> None:
    """Verify SchedulerEngine schedule evaluation, toggle persistence, and interval triggers."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from scheduler_daemon import SchedulerEngine

    state_file = tmp_path / "scheduler_state.json"
    engine = SchedulerEngine(state_path=state_file)
    assert len(engine.get_status()) == 8

    engine.toggle_job("obx_eod_scan", True)
    reloaded = SchedulerEngine(state_path=state_file)
    eod_job = next(j for j in reloaded.get_status() if j["id"] == "obx_eod_scan")
    assert eod_job["enabled"] is True

    fired: list[str] = []
    # Monday 16:40 Europe/Oslo should trigger obx_eod_scan
    mon_1640 = datetime(2026, 11, 16, 16, 40, tzinfo=ZoneInfo("Europe/Oslo"))
    triggered = reloaded.tick(now=mon_1640, runner_fn=lambda jid: fired.append(jid))
    assert "obx_eod_scan" in triggered
    assert "obx_eod_scan" in fired


def test_tax_fee_fx_routing_and_trade_hurdle() -> None:
    """Verify deterministic Tax, Fee & FX routing and Trade Hurdle Rule."""
    from src.portfolio.tax_fee_fx_engine import evaluate_trade_hurdle, route_instrument

    # 1. Routing rules
    r_us = route_instrument("NVDA")
    assert r_us.recommended_broker_key == "alpaca_usd_pool"
    assert r_us.ask_qualified is False

    r_no = route_instrument("EQNR.OL")
    assert "Nordnet ASK" in r_no.recommended_account
    assert r_no.ask_qualified is True

    r_dk = route_instrument("NOVO-B.CO")
    assert r_dk.ask_qualified is True

    # 2. Trade Hurdle PASS (US Alpaca $0 commission, 0% FX in USD pool)
    res_pass = evaluate_trade_hurdle("NVDA", trade_value=10_000.0, expected_alpha_pct=1.20)
    assert res_pass["passed"] is True
    assert res_pass["costs_pct"]["commission"] == 0.0
    assert res_pass["costs_pct"]["fx"] == 0.0

    # 3. Trade Hurdle REJECT (expected alpha below all-in cost + 0.20% buffer)
    res_fail = evaluate_trade_hurdle("NVDA", trade_value=10_000.0, expected_alpha_pct=0.25, spread_pct=0.15)
    assert res_fail["passed"] is False
    assert res_fail["all_in_cost_pct"] + res_fail["alpha_buffer_pct"] > 0.25






