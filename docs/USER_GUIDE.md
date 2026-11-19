# Brukermanual (User Guide) — AI Trading Desk (Alpaca Paper & Multi-Agent)

Denne brukermanualen beskriver hvordan du setter opp, opererer, overvåker og reviderer **AI Trading Desk** i det daglige og ukentlige arbeidet — med fokus på **Alpaca Paper Trading (S&P 500 / ETF)**, **Telegram Human-in-the-Loop (HITL)** og **3-trinns Failover**.

---

## 1. Oversikt over Brukergrensesnittene (UI)

Alle flater leser fra samme backend (`runner_server.py` på port `8000`), live Alpaca Paper API v2, og de lokale databasene (`data/obx_analytics.duckdb` og `data/hitl_orders.db`):

1. **AI Trading Desk — Hovedterminal (`index.html` på `http://localhost:8000/`):**
   * **01 Overview:** Live Alpaca Paper egenkapital, tilgjengelig cash, dagens P&L, faktisk porteføljeutvikling mot S&P 500 benchmark, og live topp-posisjoner.
   * **02 Portfolio:** Automatisk synkronisert oversikt over **Total Egenkapital**, **Realised P&L**, **Urealisert P&L**, åpne posisjoner og siste ordrer fra Alpaca Paper (oppdateres automatisk hvert 15. sekund).
   * **03 Trading:**
     * **Strategi → Telegram (Anbefalt flyt):** Velg strategi i dropdown (f.eks. *1. Liquid S&P Momentum*), skriv totalbeløp i `$ USD` — `dynamic_recommender.py` henter **live børskurser fra Alpaca**, sjekker cash-balanse, beregner ATR(14) Stop-Loss / Take-Profit og sender forslagene til **Telegram** for godkjenning.
     * **Manuell OCO Bracket-ordre:** Legg inn enkeltordre direkte (`symbol`, `qty`, `limit`, `SL`, `TP`) mot Alpaca Paper med lokal validering av ticker og SL/TP.
   * **04 Markets:** Live BBO-kurser, **Norden (OMXN40 — 40 selskaper)** med landfilter (`DK / SE / FI`), **Norge (OBX 15)** og HITL-varsler.
   * **05 Analytics:** US & Nordisk (OMXN40 / OBX 15) Multi-Universe Backtests, 6-Case matrise, AI Strategi-rådgiver, Agent Rulebook, 3-trinns Failover og **Deterministisk Tax, Fee & FX Hurdle-Motor** (`src/portfolio/tax_fee_fx_engine.py`).
   * **06 Settings:** Lokal redigering av `.env` (Alpaca Paper, Telegram HITL, Backup E-post / SMTP, Ollama/Gemini) med **demo-maskering** (`xxxx••••`) for sikker skjermdeling.

2. **Dokumentasjon i venstremenyen (alle tilgjengelig på `http://localhost:8000/`):**
   * `http://localhost:8000/rulebook` — **📜 Agent Rulebook** (`docs/trading_agent_rulebook.md`)
   * `http://localhost:8000/tax-fx` — **💰 Skatt, Gebyr & FX-Struktur** (`docs/anbefalte_opt_tax_fee_fx_struktur.md`)
   * `http://localhost:8000/guide` — **🛠️ Brukermanual** (`docs/USER_GUIDE.md`)
   * `http://localhost:8000/dummies` — **📘 Strategi for Dummies** (`docs/ai-trading-strategi-for-dummies.md`)
   * `http://localhost:8000/brukertest` — **📲 Telegram UX-Protokoll** (`docs/ui_ux_bruker_test.md`)
   * `http://localhost:8000/brukertest-nybegynner` — **🧪 Nybegynner-Brukertest** (`docs/bruker-test-nybegynner.md`)

---

## 2. Oppsett & Sikkerhetssperrer (`.env`)

1. Kopier `.env.example` til `.env` (`.env` er git-ignorert og må aldri lastes opp til GitHub).
2. Fyll inn dine lokale nøkler i `.env`:
   * **Alpaca Paper (påkrevd):** `ALPACA_API_KEY` (eller `alpaca_key`) og `ALPACA_SECRET_KEY` (eller `alpaca_secret`) fra **Paper Trading**-kontoen på `app.alpaca.markets`.
   * **Telegram HITL (påkrevd for mobilgodkjenning):** `TELEGRAM_BOT_TOKEN` og `TELEGRAM_CHAT_ID`.
   * **SMTP E-post Failover (valgfritt, Trinn 2):** `SMTP_USER`, `SMTP_PASS`, `ALERT_EMAIL_TO`.
3. **Hard Paper-Only Sperre:** Både `AlpacaClient` (`src/brokers/alpaca_client.py`) og `DynamicRecommender` (`dynamic_recommender.py`) blokkerer automatisk alle endepunkter som ikke er `https://paper-api.alpaca.markets/v2`.
4. **Automatisk `.env`-relasting:** `runner_server.py` laster `.env` på nytt fortløpende når du oppdaterer nøkler, slik at du slipper å restarte serveren etter nøkkelbytte.

---

## 3. Daglig Operasjonell Rutine (Mandag–Fredag)

### Steg 1: Start Lokal Infrastruktur
```powershell
# Terminal 1: Web Trading Desk & API Server (port 8000)
python runner_server.py

# Terminal 2: Streamlit Kontrolltårn (port 8501)
python -m streamlit run app.py --server.port=8501

# Terminal 3: Telegram Godkjenningslytter (eksekverer godkjente ordrer på Alpaca Paper)
python telegram_hitl_trader.py --listen-only
```

### Steg 2: Generer og Send Strategiordre til Telegram
1. Åpne `http://localhost:8000/` og gå til **03 Trading** (eller bruk **⚡ Ny Ordre / Trade** i venstremenyen).
2. Velg strategi i dropdown (f.eks. *1. Liquid S&P Momentum* eller *3. Aggressiv AI & Tech*) og ønsket totalbeløp i `$ USD` (f.eks. `50000`).
3. Klikk **📲 Send til Telegram for Godkjenning**.
   * Systemet verifiserer live tilgjengelig kontantbalanse hos Alpaca Paper (avviser automatisk ved utilstrekkelig cash).
   * `DynamicRecommender` dimensjonerer antall aksjer og ATR(14) Stop-Loss / Take-Profit.
   * Ordreforslagene sendes som interaktive kort til Telegram-appen din.

### Steg 3: Godkjenn via 3-Trinns Failover-Kjede
1. **Primær (Telegram):** Trykk **`✅ Godkjenn`** på mobilen → `telegram_hitl_trader.py` sender en meglernativ **OCO Bracket-ordre** (Limit + Stop-Loss + Take-Profit) til Alpaca Paper. Posisjonen dukker automatisk opp i **02 Portfolio**.
2. **Sekundær (E-post Failover):** Dersom Telegram feiler eller når timeout, sender `failover_approver.py` en HTML-epost med signerte `/approve` og `/reject`-lenker.
3. **Tertiær (Web Terminal):** Alle failover-ordrer vises også direkte i failover-panelet i `index.html` og `app.py` med direkte godkjenningsknapper.

### Steg 4: Automatisering med Windows 11 Oppgaveplanlegger
For automatisk kjøring etter faste tidspunkter (selv om nettleseren er lukket):
```powershell
powershell -ExecutionPolicy Bypass -File .\setup_windows_scheduler.ps1 -Install
powershell -ExecutionPolicy Bypass -File .\setup_windows_scheduler.ps1 -Status
```
Registrerer 4 faste oppgaver: `OBX_PreOpen_0855`, `US_PreOpen_1515`, `OBX_EOD_Scan_1640` og `Agent5_Sunday_Audit_2000`.

---

## 4. Ukentlig Revisjonsrutine (Søndag kl. 20:00 CET)

Kjør Agent 5-revisjonen (automatisk via Task Scheduler eller manuelt):
```powershell
python portfolio_monitor.py
python setup_drift_views.py
python scripts/run_weekly_audit.py
```

Kontroller tre hovedmål i **05 Analytics** / **Streamlit Fane 4**:
1. **Rullende 60-dagers Sortino Ratio (`v_rolling_strategy_drift_60d`):** Bør ligge over **1.20** (Balansert profil). Ved fall under terskel varsles `🔴 KONSEPTDRIFT_VARSEL`.
2. **Cost Drag % av Bruttogevinst:** Skal ligge **under 20 %** (på Alpaca Paper er aksjekurtasjen `$0`, men spread og slippage måles løpende).
3. **Shadow Tracking (`v_gemini_shadow_tracking_audit`):** Sammenligner **Kohort A (Alle signaler)**, **Kohort B (AI GO)** og **Kohort C (AI NO-GO)** for å sjekke at AI-risikomanageren faktisk øker risikojustert avkastning og ikke overfiltrerer pga. `MACRO_NOISE`.

---

## 5. Feilsøking (Troubleshooting)

* **`401 Client Error: Unauthorized` mot `paper-api.alpaca.markets`:**
  * Nøklene i `.env` er utløpt, regenerert eller fra feil konto. Logg inn på `app.alpaca.markets`, velg **Paper Trading** oppe til venstre, generer nytt nøkkelpar under **API Keys**, og lagre både API Key og Secret Key i `.env`.
* **`Mangler TELEGRAM_BOT_TOKEN eller TELEGRAM_CHAT_ID i .env`:**
  * Kontroller at begge verdiene er utfylt i `.env`, og pass på at det ikke kjører gamle duplikatprosesser av `runner_server.py` på port `8000`.
* **DuckDB `IOException: file is being used by another process`:**
  * Alle moduler bruker `connect_duckdb()` i `src/db/duckdb_store.py` med `read_only=True` for lesespørringer og retry-backoff på Windows.
* **Lokal Ollama eller Gemini svarer ikke:**
  * Agent 3 (`src/hitl/llm_validator.py`) er **fail-closed**: signaler settes automatisk til `NO-GO` dersom ingen risikovurdering kan hentes, og 48t/3-dagers Earnings Guardrail blokkerer alltid uavhengig av LLM-status.
