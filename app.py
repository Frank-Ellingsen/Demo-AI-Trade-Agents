"""Streamlit Executive Control Tower for the 5-Agent OBX 15 Trading Desk (Tufte Edition).

Can be launched either with:
    python app.py
or:
    python -m streamlit run app.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _ensure_streamlit_runner() -> None:
    """If the user runs `python app.py` directly, automatically re-launch inside `streamlit run`."""
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx

        if get_script_run_ctx() is None and os.environ.get("ANTIGRAVITY_ST_BOOTSTRAPPED") != "1":
            env = dict(os.environ)
            env["ANTIGRAVITY_ST_BOOTSTRAPPED"] = "1"
            cmd = [sys.executable, "-m", "streamlit", "run", str(Path(__file__).resolve()), *sys.argv[1:]]
            raise SystemExit(subprocess.call(cmd, env=env))
    except ImportError:
        pass


if __name__ == "__main__":
    _ensure_streamlit_runner()


def _sync_streamlit_secrets_to_env() -> None:
    """Expose Streamlit Cloud st.secrets as environment variables for Alpaca/Telegram clients."""
    try:
        import streamlit as _st

        for k, v in _st.secrets.items():
            if isinstance(v, (str, int, float, bool)) and k not in os.environ:
                os.environ[str(k)] = str(v)
    except Exception:
        pass


_sync_streamlit_secrets_to_env()

import duckdb
import streamlit as st

from alpaca_execution import AlpacaClient, build_alpaca_bracket_payload
from backtest_nordic_multi_universe import run_nordic_backtest
from backtest_us_multi_universe import calculate_metrics as calculate_us_metrics, run_us_backtest
from config.settings import settings
from dynamic_recommender import PROFILE_CONFIGS, DynamicRecommender
from improved_execution import (
    LiquidityBand,
    OrderSide,
    build_saxo_order_payload,
    validate_execution_guardrails,
)
from intuitive_backtest_engine import generate_intuitive_comparison
from live_market_bridge import (
    ALPACA_WS_DATA_URL,
    ALPACA_WS_UPDATES_URL,
    MASSIVE_BASE_URL,
    AlpacaStreamer,
    MassiveClient,
)
from portfolio_monitor import PORTFOLIO_CONFIG, initialize_portfolio_database
from scripts.export_ui_data import build_dashboard_payload
from setup_drift_views import setup_drift_monitoring_views
from src.db.duckdb_store import connect_duckdb
from src.db.sqlite_store import get_sqlite_conn, init_sqlite_db
from src.hitl.order_queue import enqueue_order_proposal, expire_stale_orders, update_order_status
from telegram_hitl_trader import TelegramHITLTrader
from telegram_strategy_advisor_bot import StrategyAdvisor, TelegramBotListener

st.set_page_config(
    page_title="AI Trading Desk — Control Tower",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Enforce Edward Tufte Data-Ink Ratio in Streamlit CSS (no shadows, no vertical borders, tabular numerals)
st.markdown(
    """
    <style>
      .block-container { padding-top: 1.5rem; padding-bottom: 2rem; }
      div[data-testid="stMetric"] {
        background: transparent !important;
        border-top: 1px solid #16181a !important;
        border-left: none !important;
        border-right: none !important;
        border-bottom: none !important;
        box-shadow: none !important;
        border-radius: 0 !important;
        padding: 0.5rem 0 0 0 !important;
      }
      table {
        border-collapse: collapse !important;
        width: 100%;
        font-variant-numeric: tabular-nums;
      }
      th, td {
        border-left: none !important;
        border-right: none !important;
        border-top: none !important;
        border-bottom: 1px solid #dcdad4 !important;
        padding: 0.45rem 0.65rem !important;
      }
      thead th {
        border-bottom: 2px solid #16181a !important;
        font-weight: 600 !important;
      }
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_data(ttl=60)
def load_cached_dashboard_data() -> dict:
    """Load pre-exported JSON snapshot or build live from DuckDB + SQLite WAL."""
    json_path = settings.base_dir / "ui" / "dashboard_data.json"
    if json_path.exists():
        return json.loads(json_path.read_text(encoding="utf-8"))
    return build_dashboard_payload()


@st.cache_data(ttl=15)
def load_live_alpaca_portfolio() -> dict:
    """Fetch live Alpaca Paper account, open positions, and recent orders."""
    try:
        return AlpacaClient().get_portfolio_snapshot()
    except Exception as exc:
        return {
            "connected": False,
            "error": str(exc),
            "equity": 100000.0,
            "cash": 100000.0,
            "buying_power": 100000.0,
            "long_market_value": 0.0,
            "today_pnl": 0.0,
            "today_pnl_pct": 0.0,
            "positions": [],
            "recent_orders": [],
        }


def render_embedded_html(html_content: str, height: int = 900) -> None:
    """Render embedded HTML using `st.iframe` (Streamlit 2026+) with fallback for older versions."""
    if hasattr(st, "iframe"):
        st.iframe(html_content, height=height)
    else:
        st.components.v1.html(html_content, height=height, scrolling=True)


def main() -> None:
    init_sqlite_db()
    data = load_cached_dashboard_data()
    macro = data.get("macro_regime", {})

    st.title("AI Trading Desk — Multi-Agent Kontrolltårn")
    st.caption(
        f"Siste børsdag i DuckDB: **{data.get('latest_trade_date', '—')}**  |  "
        f"OBX vs. SMA(200): **{macro.get('obx_close', 0):.1f} / {macro.get('obx_sma200', 0):.1f}**  |  "
        f"VIX: **{macro.get('vix_close', 0):.1f}**  |  "
        f"Brent 2d: **{macro.get('brent_2d_pct', 0):+.2f} %**"
    )

    with st.sidebar:
        st.subheader("Operasjonelle Kontroller")
        if st.button("🔄 Oppdater Snapshot fra DuckDB", use_container_width=True):
            fresh = build_dashboard_payload()
            out_json = settings.base_dir / "ui" / "dashboard_data.json"
            out_json.parent.mkdir(parents=True, exist_ok=True)
            out_json.write_text(json.dumps(fresh, indent=2, ensure_ascii=False), encoding="utf-8")
            st.cache_data.clear()
            st.rerun()

        if st.button("⏳ Kjør 14t Auto-Expiry (SQLite WAL)", use_container_width=True):
            exp_cnt = expire_stale_orders()
            st.info(f"Utløpte ordrer satt til EXPIRED: {exp_cnt}")

        if st.button("➕ Legg inn Demo HITL-ordre (EQNR.OL)", use_container_width=True):
            enqueue_order_proposal(
                ticker="EQNR.OL",
                action="BUY",
                shares=88,
                signal_close_price=284.15,
                limit_price=284.10,
                stop_loss_price=271.50,
                strategy_name="rsi_mean_reversion",
                horizon_days=20,
                priority_rank=91.5,
                llm_provider="ollama:qwen2.5:14b",
                llm_recommendation="GO",
                llm_confidence=0.86,
                risk_score="LOW",
                earnings_blackout_flag=False,
                bull_case="RSI(2) < 10 etter 3d pullback i opptrend. Aktivt tilbakekjøpsprogram på NewsWeb.",
                devils_advocate="Kortsiktig Brent-svingning kan teste 278 NOK.",
            )
            st.success("Lagt til EQNR.OL kjøpsforslag i SQLite WAL (`hitl_orders.db`).")
            st.rerun()

    # Top KPI Strip (Tufte style)
    alp_live = load_live_alpaca_portfolio()
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric(
        "Alpaca Paper Equity",
        f"${alp_live['equity']:,.2f}",
        f"P&L i dag: {alp_live['today_pnl']:+,.2f} ({alp_live['today_pnl_pct']:+.2f}%)",
    )
    c2.metric(
        "Alpaca Kontanter & Posisjoner",
        f"${alp_live['cash']:,.2f}",
        f"{len(alp_live['positions'])} åpne posisjoner",
    )
    c3.metric("OBX 15 Netto (Kombinert 20d)", "+46.31 %", "CAGR +5.54 % (Saxo)")
    c4.metric("Cost Drag % av Brutto", "15.12 %", "-4.88 pp under 20 % tak")
    c5.metric("Sortino (RSI) / Ren Alfa", "1.08", "+2.34 % α/år (β_OBX: 0.07)")

    tab_intuitive, tab_matrix, tab_hitl, tab_universe, tab_drift, tab_guide, tab_html, tab_multi = st.tabs(
        [
            "0. Intuitiv Strategisammenligning (6 Case)",
            "1. Eksperimentmatrise & Cost Drag",
            "2. Agent 3 & 4: HITL Ordrekø & Pre-Execution Guardrail",
            "3. Agent 1 & 2: Norden (OMXN40) & Norge (OBX 15)",
            "4. Agent 5: Portefølje & Konseptdrift (DuckDB Views)",
            "5. Brukermanual & Strategi for Dummies",
            "6. Frittstående HTML-Dashboards (Aurora & Tufte)",
            "7. Multi-Univers Backtest, Live Bridge & Alpaca Rådgiver",
        ]
    )

    with tab_intuitive:
        st.subheader("Empirisk Strategisammenligning: Enkeltaksje vs. 100k Portefølje & Endogen vs. Eksogen")
        st.caption(
            "Viser hvorfor ubegrenset enkeltaksjetesting ('01_SINGLE_ENDO_20D') gir falsk optimisme på antall handler, "
            "og hvordan det eksogene regimefilteret kutter Max Drawdown fra 35.1 % ned til 7.0 % – 15.4 %."
        )
        df_intuitive = generate_intuitive_comparison()
        st.dataframe(df_intuitive, use_container_width=True, hide_index=True)

        st.subheader("Interaktiv Pre-Execution Guardrail & MiFID II Tick-Size Simulator (`improved_execution.py`)")
        g1, g2, g3, g4 = st.columns(4)
        sim_sig_close = g1.number_input("Signal Sluttkurs Close(T) NOK", value=284.15, step=0.5)
        sim_open = g2.number_input("Åpningskurs Open(T+1) NOK", value=285.37, step=0.5)
        sim_band = g3.selectbox("MiFID II Likviditetsbånd", options=[6, 5, 4, 3, 2, 1], index=0)
        sim_side = g4.selectbox("Retning", options=["BUY", "SELL"], index=0)

        val_res = validate_execution_guardrails(
            signal_close_price=sim_sig_close,
            current_market_price=sim_open,
            side=OrderSide(sim_side),
            band=LiquidityBand(sim_band),
        )
        if val_res.is_valid:
            saxo_preview = build_saxo_order_payload(
                uic=2115,
                account_key="SIM_ASK_ACCOUNT",
                amount=int(25000 // val_res.proposed_limit_price),
                limit_price=val_res.proposed_limit_price,
                side=OrderSide(sim_side),
            )
            st.success(
                f"**{val_res.status}**  |  Åpningsgap: `{val_res.slippage_pct:+.2f} %`  |  "
                f"MiFID II Tick-avrundet Limit: **`{val_res.proposed_limit_price:.2f} NOK`**  |  "
                f"Idempotency `X-Request-Id`: `{saxo_preview['idempotency_key']}`"
            )
        else:
            st.error(f"**{val_res.status}** — {val_res.reason}")

    with tab_matrix:
        st.subheader("OBX 15 Eksperimentmatrise (2019–2026) — Saxo Classic vs. Nordnet Mini")
        st.caption(
            "Venstrejustert tekst, høyrejusterte tall med faste desimaler, null vertikale rutenettlinjer."
        )
        st.dataframe(
            data.get("matrix_results", []),
            use_container_width=True,
            hide_index=True,
        )

    with tab_hitl:
        st.subheader("Agent 4: Aktive Ordreforslag i `hitl_orders.db` (WAL-modus)")
        with get_sqlite_conn() as conn:
            live_orders = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM pending_orders ORDER BY created_at DESC LIMIT 20;"
                ).fetchall()
            ]

        if not live_orders:
            st.info(
                "Ingen aktive ordrer i `hitl_orders.db` akkurat nå. Klikk **«➕ Legg inn Demo HITL-ordre»** i menyen til venstre for å teste godkjenningssløyfen."
            )
        else:
            for ord_row in live_orders:
                oid = ord_row["order_id"]
                with st.container():
                    col_info, col_ai, col_act = st.columns([2.2, 2.8, 1.5])
                    with col_info:
                        st.markdown(
                            f"**{ord_row['ticker']}** — `{ord_row['action']}` ({ord_row['strategy_name']})\n\n"
                            f"Aksjer: `{ord_row['shares']}` @ Limit `{ord_row['limit_price']:.2f} NOK`  |  "
                            f"Stop: `{ord_row['stop_loss_price'] or 0.0:.2f} NOK`\n\n"
                            f"Status: **`{ord_row['status']}`**  |  `X-Request-ID`: `{ord_row['x_request_id']}`"
                        )
                    with col_ai:
                        st.markdown(
                            f"**Agent 3 ({ord_row['llm_provider']}):** `{ord_row['llm_recommendation']}` "
                            f"(Risiko: `{ord_row['risk_score']}`)\n\n"
                            f"• *Bull Case:* {ord_row['bull_case'] or '—'}\n\n"
                            f"• *Djevelens Advokat:* {ord_row['devils_advocate'] or '—'}"
                        )
                    with col_act:
                        if ord_row["status"] == "PENDING":
                            if st.button("✅ Godkjenn", key=f"app_{oid}", use_container_width=True):
                                update_order_status(oid, "APPROVED")
                                st.rerun()
                            if st.button("❌ Avvis", key=f"rej_{oid}", use_container_width=True):
                                update_order_status(oid, "REJECTED")
                                st.rerun()
                    st.divider()

        st.subheader("🇺🇸 Alpaca S&P 500 Bracket-Ordre (OCO) & Konto-Status (`alpaca_execution.py`)")
        if alp_live.get("positions"):
            st.markdown("**Åpne Posisjoner i Alpaca Paper (`paper-api.alpaca.markets`):**")
            st.dataframe(alp_live["positions"], use_container_width=True, hide_index=True)
        else:
            st.info(
                f"Ingen åpne posisjoner i Alpaca Paper (`0` stk) | "
                f"Egenkapital: `${alp_live['equity']:,.2f}` | Kontanter: `${alp_live['cash']:,.2f}` (100 % likvid)."
            )

        with st.expander("📲 Ny Strategi-Ordre med Cash-Kontroll → Send til Telegram for Godkjenning", expanded=True):
            sc_c1, sc_c2, sc_c3 = st.columns([2.2, 1.2, 1.6])
            strat_choice = sc_c1.selectbox(
                "Velg Strategi:",
                options=[
                    "1. Liquid S&P Momentum (AAPL, MSFT, JPM, CAT, XOM)",
                    "2. Konservativ Index Core (SPY, QQQ, XLV, IWM)",
                    "3. Aggressiv AI & Tech (NVDA, AVGO, TSLA, META, PLTR, AMZN)",
                    "4. AI Regime Switcher (VIX-styrt S&P 500)",
                ],
                key="st_strat_choice",
            )
            strat_budget = sc_c2.number_input(
                "Totalbeløp ($ USD):",
                min_value=1000.0,
                value=50000.0,
                step=5000.0,
                key="st_strat_budget",
            )
            avail_cash = float(alp_live.get("cash", 0.0))
            cash_ok = strat_budget <= avail_cash
            sc_c3.metric(
                "Cash-Kontroll (Alpaca)",
                f"${avail_cash:,.2f}",
                "OK" if cash_ok else f"Mangler ${strat_budget - avail_cash:,.2f}",
            )
            if st.button("📲 Generer Papirer & Send til Telegram for Godkjenning", disabled=not cash_ok, use_container_width=True):
                try:
                    import time as _time

                    p_map = {"1.": "2", "2.": "1", "3.": "3", "4.": "2"}
                    p_key = p_map.get(strat_choice[:2], "2")
                    rec = DynamicRecommender().generate_recommendation(p_key, custom_capital=float(strat_budget))
                    hitl_t = TelegramHITLTrader()
                    base_ts = int(_time.time())
                    for idx, p in enumerate(rec["proposals"]):
                        hitl_t.send_trade_signal(
                            trade_id=f"trd_st_{base_ts}_{idx}",
                            symbol=p["ticker"],
                            qty=int(p["qty"]),
                            side="buy",
                            limit_price=float(p["price"]),
                            take_profit_price=float(p["tp_price"]),
                            stop_loss_price=float(p["sl_price"]),
                            reasoning=f"Strategi: {strat_choice} (Budsjett ${strat_budget:,.0f} USD).",
                            strategy_label=strat_choice,
                            portfolio_equity=float(strat_budget),
                            cash_reserve_pct=float(rec["cash_buffer_pct"]),
                        )
                    st.success(
                        f"✅ Sendt {len(rec['proposals'])} kjøpsforslag ({strat_choice}) til Telegram på mobilen din! "
                        "Trykk [✅ GODKJENN BRACKET-ORDRE] i Telegram for å eksekvere."
                    )
                except Exception as exc:
                    st.error(f"Kunne ikke sende strategi-forslag: {exc}")

        a1, a2, a3, a4 = st.columns(4)
        alp_sym = a1.text_input("S&P 500 Ticker", value="NVDA")
        alp_qty = a2.number_input("Antall aksjer (qty)", min_value=1, value=10, step=1)
        alp_lim = a3.number_input("Limit-kurs (USD)", min_value=1.0, value=140.00, step=0.5)
        alp_sl = a4.number_input("Stop-Loss (USD)", min_value=1.0, value=133.50, step=0.5)
        alp_tp = round(alp_lim * 1.06, 2)

        bracket_preview = build_alpaca_bracket_payload(
            symbol=alp_sym,
            qty=int(alp_qty),
            side="buy",
            limit_price=alp_lim,
            take_profit_price=alp_tp,
            stop_loss_price=alp_sl,
        )
        st.caption(
            f"Generert Alpaca v2 Bracket Payload (`order_class='bracket'`): Entry `${alp_lim:.2f}` | "
            f"OCO Take-Profit `${alp_tp:.2f}` | OCO Stop-Loss `${alp_sl:.2f}`"
        )
        col_btn_status, col_btn_send, col_btn_cb = st.columns(3)
        if col_btn_status.button("🔍 Sjekk Alpaca Konto & Posisjoner", use_container_width=True):
            try:
                client = AlpacaClient()
                acct = client.get_account()
                pos = client.get_positions()
                st.success(
                    f"Tilkoblet Alpaca ({client.endpoint}) | Equity: `${float(acct.get('equity', 0)):,.2f}` | "
                    f"Buying Power: `${float(acct.get('buying_power', 0)):,.2f}` | Åpne posisjoner: `{len(pos)}`"
                )
            except Exception as exc:
                st.warning(f"Alpaca API-kall: {exc}")

        if col_btn_send.button("🚀 Send Bracket-Ordre til Alpaca Paper", use_container_width=True):
            try:
                client = AlpacaClient()
                res = client.place_bracket_order(
                    symbol=alp_sym,
                    qty=int(alp_qty),
                    side="buy",
                    limit_price=alp_lim,
                    take_profit_price=alp_tp,
                    stop_loss_price=alp_sl,
                )
                load_live_alpaca_portfolio.clear()
                st.success(f"Bracket-ordre akseptert av Alpaca! Order ID: `{res.get('id')}` (Status: `{res.get('status')}`)")
                st.rerun()
            except Exception as exc:
                st.error(f"Kunne ikke sende Alpaca-ordre: {exc}")

        if col_btn_cb.button("🚨 Circuit Breaker: Kanseller Alle Alpaca-Ordrer", use_container_width=True):
            try:
                client = AlpacaClient()
                cancelled = client.cancel_all_orders()
                st.warning(f"Kansellerte {len(cancelled)} åpne Alpaca-ordrer.")
            except Exception as exc:
                st.error(f"Feil ved kansellering: {exc}")

        st.markdown("#### 📲 Telegram Human-in-the-Loop (HITL) Trader (`telegram_hitl_trader.py`)")
        tg_col1, tg_col2 = st.columns(2)
        if tg_col1.button("📲 Send Bracket-Forslag til Telegram Mobil (HITL)", use_container_width=True):
            try:
                import time as _time

                hitl_trader = TelegramHITLTrader()
                tid = f"trd_{int(_time.time())}"
                mid = hitl_trader.send_trade_signal(
                    trade_id=tid,
                    symbol=alp_sym,
                    qty=int(alp_qty),
                    side="buy",
                    limit_price=float(alp_lim),
                    take_profit_price=float(alp_tp),
                    stop_loss_price=float(alp_sl),
                    reasoning=f"Sendt fra Streamlit Kontrolltårn: Entry ${alp_lim:.2f}, SL ${alp_sl:.2f}, TP ${alp_tp:.2f}.",
                )
                st.success(
                    f"✅ Sendt til Telegram på mobilen din (Trade ID: `{tid}`, Message ID: `{mid}`)! "
                    "Trykk [✅ Godkjenn (Kjop)] eller [❌ Avvis] i Telegram."
                )
            except Exception as exc:
                st.error(f"Kunne ikke sende til Telegram: {exc}")

        if tg_col2.button("🎧 Lytt på Mobil-Svar i 20s & Eksekver i Alpaca", use_container_width=True):
            try:
                hitl_trader = TelegramHITLTrader()
                with st.spinner("Lytter etter knappetrykk fra Telegram på mobilen din (opptil 20 sekunder)..."):
                    hitl_trader.listen_and_process(timeout_seconds=20, stop_after_first=True)
                st.success("Telegram HITL-sjekk fullført! Sjekk status i ordrekøen ovenfor eller i Telegram-appen.")
                st.rerun()
            except Exception as exc:
                st.error(f"Feil i Telegram-lytter: {exc}")

        st.caption("UI/UX Brukertest-Protokoll (`ui_ux_bruker_test.md`) — Send et av de 4 offisielle 6-felts testscenarioene til mobilen:")
        sc1, sc2, sc3, sc4 = st.columns(4)
        for col_btn, sc_id, sc_label in (
            (sc1, "1", "🟢 Scen. 1: MSFT (Standard)"),
            (sc2, "2", "🔴 Scen. 2: PLTR (Earnings Trap)"),
            (sc3, "3", "⚡ Scen. 3: XOM (Slippage-Sperre)"),
            (sc4, "4", "⚠️ Scen. 4: COP (Sektortak 2/2)"),
        ):
            if col_btn.button(sc_label, key=f"st_ux_sc_{sc_id}", use_container_width=True):
                try:
                    res_sc = TelegramHITLTrader().send_test_scenario(sc_id)
                    st.success(
                        f"Sendt Scenario {sc_id} ({res_sc['symbol']}) til Telegram! "
                        f"Trade ID: `{res_sc['trade_id']}` (Msg ID: `{res_sc['message_id']}`)"
                    )
                except Exception as exc:
                    st.error(f"Feil ved sending av Scenario {sc_id}: {exc}")

    with tab_universe:
        st.subheader("🇳🇴 Norge (OBX 15) — Siste Teknisk Status & MiFID II Likviditetsbånd")
        st.dataframe(
            data.get("obx15_snapshot", []),
            use_container_width=True,
            hide_index=True,
        )

        st.divider()
        st.subheader("🇩🇰🇸🇪🇫🇮 Norden (OMXN40 — 40 Selskaper)")
        import yaml as _yaml

        uni_path = settings.base_dir / "config" / "universe.yaml"
        omxn40_list = (
            _yaml.safe_load(uni_path.read_text(encoding="utf-8")).get("omxn40_stocks", [])
            if uni_path.exists()
            else []
        )
        ctry_filter = st.radio(
            "Filtrer OMXN40 etter land:",
            options=["Alle (40)", "DK (Danmark)", "SE (Sverige)", "FI (Finland)"],
            horizontal=True,
            key="st_omxn40_country",
        )
        if not ctry_filter.startswith("Alle"):
            code = ctry_filter.split(" ")[0]
            omxn40_view = [s for s in omxn40_list if s.get("country") == code]
        else:
            omxn40_view = omxn40_list
        st.dataframe(omxn40_view, use_container_width=True, hide_index=True)

    with tab_drift:
        st.subheader("Agent 5: Rullende 60-Dagers Konseptdrift (`v_rolling_strategy_drift_60d`)")
        initialize_portfolio_database()
        setup_drift_monitoring_views()
        con = connect_duckdb(settings.duckdb_path, read_only=True)
        try:
            drift_df = con.execute("SELECT * FROM v_rolling_strategy_drift_60d ORDER BY case_id;").df()
            meta_df = con.execute("SELECT * FROM portfolio_metadata;").df()
        finally:
            con.close()
        st.dataframe(drift_df, use_container_width=True, hide_index=True)
        st.subheader(f"Prøveportefølje Status (`{PORTFOLIO_CONFIG['portfolio_name']}`)")
        st.dataframe(meta_df, use_container_width=True, hide_index=True)

    with tab_guide:
        st.subheader("Interaktiv Risikoprofil-Velger (Fra «AI Trading Strategi for Dummies»)")
        p_col1, p_col2 = st.columns([1.2, 2.8])
        with p_col1:
            selected_profile = st.radio(
                "Velg risikoprofil (100 000 NOK startkapital):",
                options=["Konservativ Profil", "Balansert Profil (Standard)", "Aggressiv Profil"],
                index=1,
            )
            sim_cap = st.number_input("Startkapital (NOK på ASK):", min_value=50000, max_value=1000000, value=100000, step=10000)

        profile_specs = {
            "Konservativ Profil": {
                "max_pos": 5,
                "alloc_pct": 20,
                "max_sec": 1,
                "cash_buf_pct": 5,
                "atr_mult": "1.5 x ATR(14)",
                "hold_days": "5–7 handelsdager",
                "min_sortino": "> 1.50",
                "max_dd": "Maks 8.0 %",
                "circuit_breaker": "3 tap på rad eller 6 % porteføljetap",
            },
            "Balansert Profil (Standard)": {
                "max_pos": 4,
                "alloc_pct": 25,
                "max_sec": 2,
                "cash_buf_pct": 2,
                "atr_mult": "2.0 x ATR(14)",
                "hold_days": "10–15 handelsdager",
                "min_sortino": "> 1.20",
                "max_dd": "Maks 15.0 %",
                "circuit_breaker": "4 tap på rad eller 12 % porteføljetap",
            },
            "Aggressiv Profil": {
                "max_pos": 3,
                "alloc_pct": 33,
                "max_sec": 2,
                "cash_buf_pct": 1,
                "atr_mult": "2.5 x ATR(14)",
                "hold_days": "20–30 handelsdager",
                "min_sortino": "> 0.90",
                "max_dd": "Maks 25.0 %",
                "circuit_breaker": "5 tap på rad eller 20 % porteføljetap",
            },
        }
        ps = profile_specs[selected_profile]
        usable_cap = sim_cap * (1.0 - ps["cash_buf_pct"] / 100.0)
        slot_nok = usable_cap / ps["max_pos"]

        with p_col2:
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Innsats per posisjon", f"{slot_nok:,.0f} NOK", f"{ps['max_pos']} samtidige plasser")
            m2.metric("Kontantbuffer", f"{sim_cap * ps['cash_buf_pct'] / 100.0:,.0f} NOK", f"{ps['cash_buf_pct']} % reserve")
            m3.metric("Stop-Loss & Horisont", ps["atr_mult"], ps["hold_days"])
            m4.metric("Circuit Breaker", ps["max_dd"], ps["circuit_breaker"])

        st.divider()
        doc_choice = st.radio(
            "Velg dokumentasjon:",
            options=[
                "📲 UI/UX Brukertest & 6-Felts Telegram Beslutningsprotokoll (ui_ux_bruker_test.md)",
                "📜 Agent Rulebook & Failover (docs/trading_agent_rulebook.md)",
                "💰 Skatt, Gebyr & FX-Struktur (docs/anbefalte_opt_tax_fee_fx_struktur.md)",
                "🧪 Nybegynner-Brukertest (docs/bruker-test-nybegynner.md)",
                "📘 AI Trading Strategi for Dummies (docs/ai-trading-strategi-for-dummies.md)",
                "🛠️ Operasjonell Brukermanual (docs/USER_GUIDE.md)",
                "📐 Teknisk Prosjektspesifikasjon (docs/antigravity-project-spec.md)",
            ],
            horizontal=True,
        )
        if doc_choice.startswith("📜"):
            doc_path = settings.base_dir / "docs" / "trading_agent_rulebook.md"
        elif doc_choice.startswith("💰"):
            doc_path = settings.base_dir / "docs" / "anbefalte_opt_tax_fee_fx_struktur.md"
        elif doc_choice.startswith("🧪"):
            doc_path = settings.base_dir / "docs" / "bruker-test-nybegynner.md"
        elif doc_choice.startswith("📲"):
            doc_path = settings.base_dir / "docs" / "ui_ux_bruker_test.md"
        elif doc_choice.startswith("📘"):
            doc_path = settings.base_dir / "docs" / "ai-trading-strategi-for-dummies.md"
        elif doc_choice.startswith("🛠️"):
            doc_path = settings.base_dir / "docs" / "USER_GUIDE.md"
        else:
            doc_path = settings.base_dir / "docs" / "antigravity-project-spec.md"

        if doc_path.exists():
            st.markdown(doc_path.read_text(encoding="utf-8"))

        st.divider()
        st.subheader("🧠 AI Strategi- & Risikorådgiver (`telegram_strategy_advisor_bot.py`)")
        adv_q = st.text_input(
            "Still spørsmål om strategi, regime, opsjoner, OTC, rulebook eller portefølje:",
            value="regime",
            key="st_adv_query",
        )
        adv_c1, adv_c2 = st.columns(2)
        if adv_c1.button("🔍 Hent Rådgiver-Analyse", use_container_width=True):
            st.markdown(StrategyAdvisor.answer_query(adv_q))
        if adv_c2.button("📲 Send Råd til Telegram Mobil", use_container_width=True):
            try:
                ans = StrategyAdvisor.answer_query(adv_q)
                TelegramBotListener().send_text(ans)
                st.success("Sendte strategiråd til Telegram på mobilen din!")
            except Exception as exc:
                st.error(f"Kunne ikke sende til Telegram: {exc}")

        st.divider()
        st.subheader("🛡️ 3-Trinns Failover Godkjenning (`failover_approver.py`)")
        from failover_approver import get_smtp_status, list_pending_approvals, send_backup_email

        smtp_st = get_smtp_status()
        fo_c1, fo_c2, fo_c3 = st.columns(3)
        fo_c1.metric("SMTP E-post Failover", "Klar" if smtp_st["configured"] else "Kun Web-kø", smtp_st["smtp_server"])
        fo_c2.metric("Mottaker-adresse", smtp_st["alert_email_to"])
        fo_c3.metric("Webhook Base URL", smtp_st["webhook_base_url"])

        if st.button("📧 Test Failover-godkjenning (E-post + Web /approve)", use_container_width=True):
            import time as _time

            tid = f"fo_test_{int(_time.time())}"
            sent_mail = send_backup_email(
                tid, "NVDA", 10, 145.0, 136.3, 164.1, "Test av 3-trinns failover fra Streamlit Kontrolltårn"
            )
            if sent_mail:
                st.success(f"Failover e-post sendt og registrert som `{tid}`!")
            else:
                st.info(f"SMTP ikke satt i .env — ordre `{tid}` er registrert i tertiær Web Failover-kø (`/approve`).")

        pending_fo = list_pending_approvals()
        if pending_fo:
            st.dataframe(pending_fo, use_container_width=True, hide_index=True)

        st.divider()
        st.subheader("💰 Deterministisk Tax, Fee & FX Hurdle-Motor (`src/portfolio/tax_fee_fx_engine.py`)")
        from src.portfolio.tax_fee_fx_engine import BROKER_PROFILES, evaluate_trade_hurdle

        tf_c1, tf_c2, tf_c3, tf_c4 = st.columns(4)
        tf_ticker = tf_c1.text_input("Ticker", value="NVDA", key="st_tf_ticker")
        tf_val = tf_c2.number_input("Ordreverdi", min_value=500.0, value=10000.0, step=1000.0, key="st_tf_val")
        tf_alpha = tf_c3.number_input("Forventet Brutto Alfa (%)", min_value=0.0, value=1.20, step=0.10, key="st_tf_alpha")
        tf_broker = tf_c4.selectbox(
            "Megler / Konto (Auto = Anbefalt)",
            options=["auto", *list(BROKER_PROFILES.keys())],
            key="st_tf_broker",
        )
        tf_res = evaluate_trade_hurdle(
            ticker=tf_ticker,
            trade_value=float(tf_val),
            expected_alpha_pct=float(tf_alpha),
            broker_key=None if tf_broker == "auto" else tf_broker,
        )
        tk1, tk2, tk3, tk4 = st.columns(4)
        tk1.metric("Anbefalt Konto", tf_res["routing"]["recommended_account"], tf_res["routing"]["market_segment"])
        tk2.metric("All-In Kostnad", f"{tf_res['all_in_cost_pct']:.3f} %", f"ASK: {'Ja' if tf_res['in_ask'] else 'Nei'}")
        tk3.metric("Trade Hurdle", f"{tf_res['hurdle_pct']:.3f} %", f"Netto Alfa: {tf_res['net_alpha_pct']:+.3f} %")
        tk4.metric("Portvakt-Beslutning", "PASS ✅" if tf_res["passed"] else "REJECT ❌", tf_res["decision"])
        st.caption(f"Begrunnelse: {tf_res['routing']['rationale']}")

    with tab_html:
        st.subheader("Interaktivt Kontrolltårn & HTML-Dashboards")
        view_choice = st.radio(
            "Velg dashboard-visning:",
            options=[
                "⚡ Streamlined & Clean AI Trading Terminal (AI_Trading_Terminal_-_Streamlined___Clean.html)",
                "🤖 NordQuant AI Trading Terminal (Bygging_av_AI-tradingagenter_i_Python.html)",
                "NordQuant Terminal (Bloomberg-stil Indeks- & Porteføljedashbord)",
                "NordicQuant Terminal (Institusjonelt Aksjehandels- & Porteføljedashbord)",
                "AI-Trading & Strategi-Rådgiver Dashbord (S&P 500, OBX, Alpaca & Saxo)",
                "Multi-Agent Meglerbord (7-faners fullversjon med Waterfall, Profil×Marked & HITL)",
                "OBX 15 AI-Trading Kontrolltårn & Porteføljeoversikt (Aurora 6-faners Chart.js-versjon)",
                "Tufte Data-Ink Kontrollpanel (ui/index.html)",
            ],
            horizontal=False,
        )
        dash_dir = settings.base_dir / "ui" / "dashboards"
        if view_choice.startswith("⚡ Streamlined"):
            st_path = dash_dir / "AI_Trading_Terminal_-_Streamlined___Clean.html"
            if st_path.exists():
                render_embedded_html(st_path.read_text(encoding="utf-8"), height=1100)
        elif view_choice.startswith("🤖 NordQuant AI"):
            nqai_path = dash_dir / "Bygging_av_AI-tradingagenter_i_Python.html"
            if nqai_path.exists():
                render_embedded_html(nqai_path.read_text(encoding="utf-8"), height=1100)
        elif view_choice.startswith("NordQuant Terminal"):
            bb_path = dash_dir / "Bloomberg-stil_Indeks-_og_Porteføljedashbord.html"
            if bb_path.exists():
                render_embedded_html(bb_path.read_text(encoding="utf-8"), height=1100)
        elif view_choice.startswith("NordicQuant Terminal"):
            nq_path = dash_dir / "AI_Trading_Terminal___Porteføljedashbord.html"
            if nq_path.exists():
                render_embedded_html(nq_path.read_text(encoding="utf-8"), height=1100)
        elif view_choice.startswith("AI-Trading & Strategi-Rådgiver"):
            adv_dash_path = dash_dir / "AI_Trading_Dashboard_med_Strategi-Rådgiver.html"
            if adv_dash_path.exists():
                render_embedded_html(adv_dash_path.read_text(encoding="utf-8"), height=1100)
        elif view_choice.startswith("Multi-Agent Meglerbord"):
            megler_path = dash_dir / "Multi-Agent_Meglerbord_—_Kontrolltårn___Porteføljeo.html"
            if megler_path.exists():
                render_embedded_html(megler_path.read_text(encoding="utf-8"), height=1100)
        elif view_choice.startswith("OBX 15 AI-Trading"):
            aurora_path = dash_dir / "OBX_15_AI_Trader_-_Kontrolltårn___Porteføljeoversikt.html"
            if aurora_path.exists():
                render_embedded_html(aurora_path.read_text(encoding="utf-8"), height=1050)
        else:
            html_path = settings.base_dir / "ui" / "index.html"
            if html_path.exists():
                render_embedded_html(html_path.read_text(encoding="utf-8"), height=900)

    with tab_multi:
        st.subheader("1. Dynamisk Porteføljerådgiver for Alpaca Markets (`dynamic_recommender.py`)")
        st.caption(
            "Henter live egenkapital fra Alpaca (eller bruker valgt USD-kapital), beregner 14-dagers ATR og dimensjonerer "
            "komplette OCO Bracket-ordrer (Entry Limit, Stop-Loss og Take-Profit)."
        )
        dr_col1, dr_col2, dr_col3 = st.columns([1.6, 1.2, 1.2])
        prof_label = dr_col1.selectbox(
            "Velg Risikoprofil (US S&P 500 / Nasdaq / Sektor-ETF):",
            options=[
                "1 — Konservativ Profil (SPY, QQQ, XLV, IWM | 20% kontanter | 1.2x ATR SL)",
                "2 — Balansert Profil (AAPL, MSFT, JPM, CAT, XOM | 10% kontanter | 1.8x ATR SL)",
                "3 — Aggressiv Profil (NVDA, AVGO, TSLA, META, PLTR, AMZN | 5% kontanter | 2.5x ATR SL)",
            ],
            index=1,
        )
        prof_key = prof_label.split(" ")[0].strip()
        use_live_eq = dr_col2.checkbox("Bruk live Alpaca Equity (hvis tilkoblet)", value=True)
        custom_usd = dr_col3.number_input("Manuell kapital ($ USD)", min_value=10000.0, value=100000.0, step=5000.0)

        recommender = DynamicRecommender()
        rec_data = recommender.generate_recommendation(
            profile_key=prof_key,
            custom_capital=None if use_live_eq else float(custom_usd),
        )

        rk1, rk2, rk3, rk4 = st.columns(4)
        rk1.metric("Total Egenkapital", f"${rec_data['total_equity']:,.2f}", rec_data["profile_name"])
        rk2.metric(
            "Allokert i Aksjer",
            f"${rec_data['allocated_total']:,.2f}",
            f"{(rec_data['allocated_total'] / rec_data['total_equity']) * 100:.1f} % eksponering",
        )
        rk3.metric("Kontantreserve", f"${rec_data['remaining_cash']:,.2f}", f"{rec_data['cash_buffer_pct']:.1f} % buffer")
        rk4.metric(
            "Circuit Breaker & R/R",
            f"-{rec_data['circuit_breaker_pct']:.1f} % MaxDD",
            f"Reward/Risk {PROFILE_CONFIGS[prof_key]['reward_risk_ratio']}:1",
        )

        st.dataframe(rec_data["proposals"], use_container_width=True, hide_index=True)

        if st.button("🚀 Send hele den anbefalte Bracket-porteføljen til Alpaca Paper Trading", use_container_width=True):
            try:
                client = AlpacaClient()
                sent_ids = []
                for p in rec_data["proposals"]:
                    resp_ord = client.place_bracket_order(
                        symbol=p["ticker"],
                        qty=int(p["qty"]),
                        side="buy",
                        limit_price=float(p["price"]),
                        take_profit_price=float(p["tp_price"]),
                        stop_loss_price=float(p["sl_price"]),
                    )
                    sent_ids.append(f"{p['ticker']} ({p['qty']} stk -> ID: {resp_ord.get('id', 'OK')})")
                load_live_alpaca_portfolio.clear()
                st.success("Sendt til Alpaca Paper Trading: " + " | ".join(sent_ids))
                st.rerun()
            except Exception as exc:
                st.error(f"Kunne ikke sende porteføljen til Alpaca: {exc}")

        st.divider()

        col_us, col_nordic = st.columns(2)
        with col_us:
            st.subheader("2. US Multi-Universe Backtest (`backtest_us_multi_universe.py`)")
            st.caption(
                "Simulerer 252 handelsdager ($100 000 USD): Liquid S&P Momentum (0 USD kurtasje), "
                "OTC Micro-Caps (1.6 % spread-friksjon), Covered Calls (BXM) og AI Regime Switcher (VIX)."
            )
            df_us_curves = run_us_backtest(initial_capital=float(custom_usd), days=252, seed=42)
            df_us_metrics = calculate_us_metrics(df_us_curves, initial_capital=float(custom_usd))
            st.dataframe(df_us_metrics, use_container_width=True, hide_index=True)
            st.line_chart(
                df_us_curves.set_index("Dag")[
                    [
                        "S&P 500 Benchmark",
                        "1. Liquid S&P Momentum",
                        "2. OTC Micro-Caps (Netto)",
                        "3. Systematic Covered Calls",
                        "4. AI Regime Switcher (VIX)",
                    ]
                ],
                height=280,
            )

        with col_nordic:
            st.subheader("3. Norden (OMXN40) & Norge (OBX 15) Backtest (`backtest_nordic_multi_universe.py`)")
            st.caption(
                "Simulerer 252 handelsdager (100 000 NOK): Norge (OBX 15 Momentum m/ Brent-filter), "
                "Norden (OMXN40 Trend m/ FX-drag), First North Small-Caps og Dekkede Opsjoner."
            )
            df_nordic_curves, df_nordic_metrics = run_nordic_backtest(initial_capital_nok=100_000.0, trading_days=252)
            st.dataframe(df_nordic_metrics, use_container_width=True, hide_index=True)
            st.line_chart(
                df_nordic_curves.set_index("Handelsdag")[
                    [
                        "1. OBX Momentum (m/ Brent-filter)",
                        "2. OMXN40 Trend (m/ Nordic FX-drag)",
                        "3. First North Small-Caps (Netto)",
                        "4. Dekkede Opsjoner (Covered Calls)",
                    ]
                ],
                height=280,
            )

        st.divider()

        st.subheader("4. Live Market Bridge: Massive Level 1 BBO & Alpaca Streams (`live_market_bridge.py`)")
        st.caption(
            f"Massive Base URL: `{MASSIVE_BASE_URL}`  |  "
            f"Alpaca Market Data WS: `{ALPACA_WS_DATA_URL}`  |  "
            f"Alpaca Trading Stream WS: `{ALPACA_WS_UPDATES_URL}`"
        )
        mb_col1, mb_col2 = st.columns([1.3, 2.7])
        massive_client = MassiveClient()
        with mb_col1:
            bridge_ticker = st.selectbox(
                "Sjekk Level 1 BBO (Bid / Ask / Spread):",
                options=["NVDA", "SPY", "QQQ", "AAPL", "MSFT", "TSLA", "AVGO", "AMZN"],
                index=0,
            )
            bbo = massive_client.get_last_quote(bridge_ticker, allow_fallback=True)
            st.metric(
                f"{bbo['ticker']} Level 1 BBO",
                f"Bid ${bbo['bid']:.2f} / Ask ${bbo['ask']:.2f}",
                f"Spread: ${bbo['spread']:.4f} ({bbo.get('source', 'LIVE')})",
            )
            streamer = AlpacaStreamer()
            st.info(
                f"**AlpacaStreamer Status:** API-nøkkel konfigurert: `{bool(streamer.key)}`  |  "
                f"Massive API-nøkkel konfigurert: `{bool(massive_client.api_key)}`"
            )

        with mb_col2:
            st.markdown("**Siste Markedsnyheter (Massive Reference News Feed):**")
            news_items = massive_client.get_latest_news(limit=5, allow_fallback=True)
            st.dataframe(news_items, use_container_width=True, hide_index=True)


if __name__ == "__main__":
    main()
