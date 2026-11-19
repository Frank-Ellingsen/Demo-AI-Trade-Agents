# AI Trading Desk — Multi-Agent Trading & Portfolio System

A local-first, quantitative and AI-supervised trading desk built with **Python**, **DuckDB** (analytics & shadow tracking), **SQLite WAL** (transactional Human-in-the-Loop order queue), **Alpaca Paper Trading API v2** (native OCO Bracket Orders), and a **3-tier Human-in-the-Loop (HITL) failover approval chain**.

---

## Core Design Principles

1. **100% Deterministic Quant Math:** Indicators (`RSI(2)`, `SMA(200)`, `ATR(14)`, 12–1m momentum), position sizing, cash checks, and OCO Stop-Loss / Take-Profit brackets are calculated deterministically in Python/DuckDB.
2. **Qualitative AI Risk Officer (Fail-Closed):** Local **Ollama** (with optional **Gemini** fallback) acts as *Devil's Advocate* to flag earnings traps (`EARNINGS_SOON`) and company shocks (`COMPANY_SHOCK`). If the LLM is unavailable, signals fail closed (`NO-GO`).
3. **Strict Paper-Only Safeguard:** `AlpacaClient` (`src/brokers/alpaca_client.py`) and `DynamicRecommender` (`dynamic_recommender.py`) enforce `https://paper-api.alpaca.markets/v2` and block live endpoints by default.
4. **3-Tier Failover Approval Chain:** No trade executes without explicit human approval:
   - **Tier 1 (Primary):** Telegram interactive inline buttons (`✅ Godkjenn` / `❌ Avvis`).
   - **Tier 2 (Secondary):** Automated HTML failover email (`failover_approver.py`) with signed `/approve` and `/reject` links.
   - **Tier 3 (Tertiary):** Built-in Web Terminal approval queue (`http://localhost:8000/`).

---

## Multi-Agent Architecture

| Agent | Role | Key Modules | Responsibility |
| :--- | :--- | :--- | :--- |
| **Agent 1** | *Quantitative Signal Engine* | `src/signals/`, `backtest_us_multi_universe.py` | Scans S&P 500 / ETF / OBX universes (`P > SMA200`, `RSI(2)`, 12–1m momentum, VIX & Brent regime filters). |
| **Agent 2** | *News & Sentiment Scraper* | `src/data_ingestion/`, `live_market_bridge.py` | Aggregates market news, NewsWeb/SEC notices, and enforces the 48h/3-day Earnings Guardrail. |
| **Agent 3** | *AI Risk Officer* | `src/hitl/llm_validator.py`, `dynamic_recommender.py` | ATR(14) position sizing, sector caps (max 2 positions / 35%), portfolio circuit breaker, and Devil's Advocate veto. |
| **Agent 4** | *Execution & HITL Broker* | `alpaca_execution.py`, `telegram_hitl_trader.py`, `failover_approver.py` | Routes approved orders to **Alpaca Paper API v2** as native OCO Bracket Orders (`entry + SL + TP`) with live cash verification. |
| **Agent 5** | *Performance Auditor* | `src/analytics/`, `portfolio_monitor.py`, `setup_drift_views.py` | Return attribution, multi-factor OLS regression, cost-drag tracking, and Shadow Tracking (Cohort A/B/C). |

---

## Supported Strategies & Risk Profiles (Alpaca Paper)

| Profile / Strategy | Universe | Cash Buffer | ATR Stop-Loss | Reward / Risk | Circuit Breaker |
| :--- | :--- | ---: | ---: | ---: | ---: |
| **1. Conservative Index Core** | `SPY, QQQ, XLV, IWM` | 20% | `1.2 × ATR(14)` | `2.0 : 1` | `-4.0%` |
| **2. Balanced Multi-Sector Momentum** | `AAPL, MSFT, JPM, CAT, XOM` | 10% | `1.8 × ATR(14)` | `2.2 : 1` | `-6.0%` |
| **3. Aggressive AI & Tech Outliers** | `NVDA, AVGO, TSLA, META, PLTR, AMZN` | 5% | `2.5 × ATR(14)` | `2.5 : 1` | `-8.0%` |
| **4. AI Regime Switcher (VIX)** | `SPY, QQQ, Covered Calls, T-Bills` | Dynamic | VIX `< 18` / `18–24` / `> 24` | Asymmetric | Dynamic |

---

## Dashboards & Local Endpoints

When running `python runner_server.py` (port `8000`) and Streamlit (port `8501`):

| Interface | URL | Description |
| :--- | :--- | :--- |
| **AI Trading Desk (Main)** | `http://localhost:8000/` | 5-category Aurora workspace: Overview, Portfolio (live Alpaca sync), Strategy→Telegram Order Entry, Markets, Analytics & Failover. |
| **Streamlit Control Tower** | `http://localhost:8501/` | Interactive Python control tower (`app.py`) with live Alpaca KPIs, backtests, Rulebook, and Failover controls. |
| **Streamlined Clean Terminal** | `http://localhost:8000/streamlined` | Minimalist Aurora trading terminal (`ui/dashboards/AI_Trading_Terminal_-_Streamlined___Clean.html`). |
| **NordQuant (Bloomberg)** | `http://localhost:8000/bloomberg` | Index & portfolio terminal (`ui/dashboards/Bloomberg-stil_Indeks-_og_Porteføljedashbord.html`). |
| **NordicQuant Terminal** | `http://localhost:8000/terminal` | Institutional equity & portfolio dashboard (`ui/dashboards/AI_Trading_Terminal___Porteføljedashbord.html`). |
| **Strategy Advisor Dashboard** | `http://localhost:8000/strategi-radgiver` | Strategy comparison & advisor dashboard (`ui/dashboards/AI_Trading_Dashboard_med_Strategi-Rådgiver.html`). |
| **Agent Rulebook** | `http://localhost:8000/rulebook` | Rendered operative rulebook (`docs/trading_agent_rulebook.md`). |

---

## Project Structure

```text
.
├── index.html                          # Main AI Trading Desk Web UI
├── app.py                              # Streamlit Control Tower (port 8501)
├── runner_server.py                    # Local Execution & REST API Server (port 8000)
├── alpaca_execution.py                 # Alpaca Paper API v2 facade (OCO Bracket Orders)
├── dynamic_recommender.py              # ATR(14) Portfolio & Risk Profile Generator (Profiles 1–3)
├── backtest_us_multi_universe.py       # US Multi-Universe Backtester (252-day simulation)
├── backtest_nordic_multi_universe.py   # Nordic (OMXN40) & Norway (OBX 15) Multi-Universe Backtester
├── telegram_strategy_advisor_bot.py    # Strategy Advisor & Telegram HITL Signal Bot
├── telegram_hitl_trader.py             # Telegram Inline-Button Callback Listener -> Alpaca Paper
├── failover_approver.py                # 3-Tier Failover Engine (Email SMTP + /approve & /reject)
├── scheduler_daemon.py                 # Scheduled Jobs & Event Triggers
├── setup_windows_scheduler.ps1         # Windows 11 Task Scheduler installer
├── .github/workflows/ci.yml            # GitHub Actions CI (pytest)
├── config/                             # YAML universe, strategy, and broker configs
├── src/                                # Core packages (signals, brokers, db, hitl, portfolio, analytics)
├── ui/
│   ├── index.html                      # Tufte Data-Ink Analytics UI
│   └── dashboards/                     # Standalone HTML dashboards
├── docs/                               # Rulebook, User Guide, Strategy Guide, UX Protocol, Spec
├── scripts/                            # Helper scripts & notebooks
└── tests/                              # Unit & integration test suite (31 tests)
```

---

## Quick Start

### 1. Install Dependencies & Configure Environment
```bash
pip install -r requirements.txt
cp .env.example .env
```
Fill in your local credentials in `.env` (never commit `.env`):
- `ALPACA_API_KEY` & `ALPACA_SECRET_KEY` (Alpaca **Paper** account)
- `TELEGRAM_BOT_TOKEN` & `TELEGRAM_CHAT_ID`
- Optional: `SMTP_USER` & `SMTP_PASS` for Tier-2 email failover

### 2. Run the Test Suite
```bash
python -m pytest -v
```

### 3. Start the Servers
```bash
# Terminal 1: Web Trading Desk & API Server (port 8000)
python runner_server.py

# Terminal 2: Streamlit Control Tower (port 8501)
python -m streamlit run app.py --server.port=8501

# Terminal 3 (optional): Telegram Approve/Reject Listener
python telegram_hitl_trader.py --listen-only
```

### 4. Optional: Register Windows 11 Scheduled Tasks
```powershell
powershell -ExecutionPolicy Bypass -File .\setup_windows_scheduler.ps1 -Install
```

---

## Documentation

- [docs/trading_agent_rulebook.md](docs/trading_agent_rulebook.md) — Operative Agent 1–4 veto rules, strategy matrix, and 3-tier failover protocol.
- [docs/USER_GUIDE.md](docs/USER_GUIDE.md) — Daily operations, Sunday audit, risk profiles, and troubleshooting.
- [docs/ai-trading-strategi-for-dummies.md](docs/ai-trading-strategi-for-dummies.md) — Strategy & risk profile guide.
- [docs/ui_ux_bruker_test.md](docs/ui_ux_bruker_test.md) — Telegram HITL UX protocol.
- [agents.md](agents.md) & [docs/risk.md](docs/risk.md) — Multi-agent architecture and factor risk decomposition.

---

*Disclaimer: This repository is built for educational, quantitative research, and **Paper Trading** workflows. Always verify risk parameters and broker safeguards before any live capital deployment.*
