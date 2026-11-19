# AI Trading Desk — Multi-Agent Trading & Portfolio System

A local-first, quantitative and AI-supervised trading desk built with **Python**, **DuckDB** (analytics & shadow tracking), **SQLite WAL** (transactional Human-in-the-Loop order queue), **Alpaca Paper Trading API v2** (native OCO Bracket Orders), and a **3-tier Human-in-the-Loop (HITL) failover approval chain** (Telegram → Email → Web Terminal).

---

## Security & Paper-Only Safeguards

- **Strict Paper-Only Lock:** `AlpacaClient` (`src/brokers/alpaca_client.py`) and `DynamicRecommender` (`dynamic_recommender.py`) enforce `https://paper-api.alpaca.markets/v2` and reject live endpoints by default.
- **Fail-Closed AI Risk Officer:** If both local Ollama and optional Gemini fallback are unavailable, trade signals default to `NO-GO`.
- **Secrets Hygiene:** Copy `.env.example` to `.env` locally. `.env` and all local `.db`/`.duckdb` files are git-ignored and must never be committed.

---

## Project Structure

```text
.
├── index.html                          # Main AI Trading Desk Web UI (Aurora 5-category layout)
├── app.py                              # Streamlit Control Tower (port 8501)
├── runner_server.py                    # Local Execution & API Server (port 8000)
├── alpaca_execution.py                 # Alpaca Paper API v2 facade (OCO Bracket Orders)
├── dynamic_recommender.py              # ATR(14) Portfolio & Risk Profile Generator (Profiles 1–3)
├── backtest_us_multi_universe.py       # US Multi-Universe Backtester (S&P 500, Momentum, OTC, Calls, VIX)
├── backtest_nordic_multi_universe.py   # Nordic Multi-Universe Backtester (OBX 15, OMXS30, First North)
├── telegram_strategy_advisor_bot.py    # Strategy Advisor & Telegram HITL Signal Bot
├── telegram_hitl_trader.py             # Telegram Inline-Button Callback Listener -> Alpaca Paper
├── failover_approver.py                # 3-Tier Failover Engine (Email SMTP + /approve & /reject webhook)
├── scheduler_daemon.py                 # Scheduled Jobs & Event Triggers
├── setup_windows_scheduler.ps1         # Windows 11 Task Scheduler installer
├── src/                                # Core modular packages (signals, brokers, db, hitl, portfolio, analytics)
├── ui/
│   ├── index.html                      # Tufte Data-Ink Analytics UI
│   └── dashboards/                     # Standalone HTML terminals (Streamlined, Bloomberg, NordicQuant, Aurora)
├── docs/                               # Rulebook, User Guide, Strategy for Dummies, UX Protocol, Spec
├── scripts/                            # Helper scripts & notebooks
└── tests/                              # Unit & integration test suite (pytest)
```

---

## Quick Start

### 1. Install Dependencies & Configure `.env`
```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env and add your Alpaca Paper + Telegram credentials
```

### 2. Run the Test Suite
```bash
python -m pytest -v
```

### 3. Launch the Desk
- **Web Trading Desk & API Server (port `8000`):**
  ```bash
  python runner_server.py
  ```
  - Main Desk: `http://localhost:8000/`
  - Streamlined Clean Terminal: `http://localhost:8000/streamlined`
  - Agent Rulebook: `http://localhost:8000/rulebook`
- **Streamlit Control Tower (port `8501`):**
  ```bash
  python -m streamlit run app.py --server.port=8501
  ```
- **Windows Task Scheduler (Optional background automation):**
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
