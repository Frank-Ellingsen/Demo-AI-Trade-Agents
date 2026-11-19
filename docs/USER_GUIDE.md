# Brukermanual (User Guide) — OBX 15 AI-Trading Kontrolltårn

Denne brukermanualen beskriver hvordan du opererer, overvåker og reviderer **OBX 15 Multi-Agent Meglerbordet** i det daglige og ukentlige arbeidet.

---

## 1. Oversikt over Brukergrensesnittene (UI)

Systemet har tre komplementære flater som alle leser fra de samme lokale databasene (`data/obx_analytics.duckdb` og `data/hitl_orders.db`):

1. **Streamlit Kontrolltårn ([app.py](file:///c:/Users/frank/Desktop/AI%20trader/app.py) på `http://localhost:8501`):**
   * **Fane 0:** *Intuitiv Strategisammenligning (6 Case)* + Interaktiv Pre-Execution Guardrail & MiFID II Tick-Size Simulator.
   * **Fane 1:** *Eksperimentmatrise & Cost Drag* (Full 2019–2026 sammenligning mellom Saxo Classic og Nordnet Mini).
   * **Fane 2:** *Agent 3 & 4: HITL Ordrekø (SQLite WAL)* — Godkjenn eller avvis ventende kjøpsforslag direkte med ett klikk.
   * **Fane 3:** *Agent 1 & 2: OBX 15 Univers* — Siste tekniske verdier (`RSI(2)`, 3d endring, `SMA200`-status og MiFID II likviditetsbånd).
   * **Fane 4:** *Agent 5: Portefølje & Konseptdrift* — Viser sanntidsdata fra `v_rolling_strategy_drift_60d` og `portfolio_metadata`.
   * **Fane 5:** *Brukermanual & Strategi for Dummies* — Innebygd interaktiv risikoprofil-kalkulator (Konservativ / Balansert / Aggressiv), univers- og opsjonsguide, samt oppstartssjekkliste.
   * **Fane 6:** *HTML-Dashboards* — Bytt direkte mellom **Aurora 5-faners Kontrolltårn** og **Tufte Data-Ink Kontrollpanel**.

2. **Mission Control & Execution Room ([index.html](file:///c:/Users/frank/Desktop/AI%20trader/index.html) via `python runner_server.py` på `http://localhost:8000/index.html`):**
   * Lar deg kjøre de 4 operasjonelle stegene enkeltvis eller i sekvens med sanntids terminal-logg (`STDOUT`/`STDERR`) i nettleseren.

3. **Aurora 5-Faners Kontrolltårn ([OBX_15_AI_Trader_-_Kontrolltårn___Porteføljeoversikt.html](file:///c:/Users/frank/Desktop/AI%20trader/OBX_15_AI_Trader_-_Kontrolltårn___Porteføljeoversikt.html) på `http://localhost:8000/kontrolltarn`):**
   * Interaktivt analyse- og porteføljedashboard med regel-sandkasse, Chart.js attribuering, rullende konseptdrift, gebyrsimulator og aksjeinspektør med CSV-eksport.

---

## 2. Daglig Operasjonell Rutine (Mandag–Fredag)

### Steg 1: Etter børsslutt på Oslo Børs (kl. 16:40 CET)
Kjør den daglige skanneren (enten via knappen **«Kjør Steg 2»** i Mission Control eller i terminalen):
```powershell
python run_daily_scan.py
```
**Hva skjer under panseret?**
1. **Utløpsvakt:** Alle ubesvarte kjøpsforslag i `hitl_orders.db` som er eldre enn **14 timer** settes automatisk til `EXPIRED`.
2. **Kvantitativ siling (Agent 1):** Sjekker hvilke av de 15 OBX-aksjene som har $\ge 50\text{ MNOK}$ i 20-dagers snittomsetning, ligger over `SMA(200)`, passerer de eksogene makrofiltrene (`OBX > SMA200`, `VIX < 25`, `Brent 2d`), og utløser enten `rsi_mean_reversion` eller `trend_breakout`.
3. **Hendelses- og AI-kontroll (Agent 2 & 3):** Henter siste 72 timers børsmeldinger fra NewsWeb RSS og sjekker kvartalskalenderen (`corporate_events`). Hvis kvartalstall er under 3 handelsdager unna, blokkeres signalet deterministisk (`EARNINGS_SOON`). Ellers vurderer lokal Ollama / Gemini halerisiko og logger resultatet i `signal_evaluations`.
4. **Ordrekø (Agent 4):** Runder foreslått limit-kurs og ATR stop-loss til gyldig **Euronext MiFID II tick-size** og legger ordren inn som `PENDING` i `data/hitl_orders.db`.

### Steg 2: Human-in-the-Loop Godkjenning (Før kl. 08:40 CET neste morgen)
* Du mottar ordrekortet på Telegram (eller ser det under **Fane 2 i Streamlit Kontrolltårnet**).
* **Kjøpsordrer (`BUY`):** Krever at du eksplisitt trykker **`[✅ Godkjenn]`**. Gjør du ingenting innen 14 timer, utløper ordren automatisk uten at kapital eksponeres.
* **Salgsordrer / Stop-Loss (`SELL`):** Settes automatisk til `AUTO_APPROVED` slik at posisjoner alltid beskyttes på nedsiden selv om du er i et møte.

### Steg 3: Børsåpning kl. 09:00 CET (Åpningsgap- og Slippage-vakt)
* Når børsen åpner kl. 09:00 CET, sjekker [stream_execution_bridge.py](file:///c:/Users/frank/Desktop/AI%20trader/stream_execution_bridge.py) (via [saxo_streamer.py](file:///c:/Users/frank/Desktop/AI%20trader/saxo_streamer.py)) åpningskursen mot gårsdagens sluttkurs:
  * Dersom aksjen åpner mer enn **$+1.5\ \%$ over** (eller **$-2.0\ \%$ under**) signalets sluttkurs, avvises ordren automatisk (`REJECTED_BY_SLIPPAGE_GUARD`) for å unngå *adverse selection*.
  * Dersom åpningen er innenfor båndet, sendes en MiFID II tick-avrundet `DayOrder` Limit-ordre til Saxo OpenAPI med unik `X-Request-Id` (15-sekunders idempotensvern) og rate limit på maks 1 ordre/sekund.

---

## 3. Ukentlig Revisjonsrutine (Hver Søndag Kveld)

Hver søndag kveld kjører du Agent 5-revisjonen:
```powershell
python portfolio_monitor.py
python setup_drift_views.py
python scripts/run_weekly_audit.py
```

Kontroller følgende tre styringsparametre (også synlige i **Fane 4 i Streamlit**):
1. **Rullende 60-dagers Sortino Ratio (`v_rolling_strategy_drift_60d`):**
   * Skal ligge over **1.20** (Balansert profil). Viser status `🔴 KONSEPTDRIFT_VARSEL` dersom strategiens statistiske edge er svekket.
2. **Cost Drag % av Bruttogevinst:**
   * Samlet kurtasje og spread skal utgjøre **under 20 %** av bruttogevinsten. På en 100 000 NOK portefølje med 4 posisjoner (25 000 NOK per handel) ligger Saxo Classic på ~12–15 %, mens Nordnet Mini ofte bikker 20–27 % på korte swing-handler.
3. **Shadow Tracking & Paranoia-sjekk (`v_gemini_shadow_tracking_audit` & `v_gemini_rejection_reasons`):**
   * Sammenligner **Kohort A (Alle signaler)**, **Kohort B (AI GO)** og **Kohort C (AI NO-GO)**.
   * Sjekk spesielt avslagsårsaken `MACRO_NOISE`: Hvis handler som AI-en avviste pga. generell makrostøy har høy vinnerrate i skyggeporteføljen (`⚠️ PARANOIA`), skal systemprompten i [src/hitl/llm_validator.py](file:///c:/Users/frank/Desktop/AI%20trader/src/hitl/llm_validator.py) strammes inn til kun å avslå på harde selskapshendelser (`EARNINGS_SOON` og `COMPANY_SHOCK`).

---

## 4. Velge Risikoprofil (Konservativ vs. Balansert vs. Aggressiv)

I **Fane 5 i Streamlit (`app.py`)** og i [ai-trading-strategi-for-dummies.md](file:///c:/Users/frank/Desktop/AI%20trader/ai-trading-strategi-for-dummies.md) kan du velge mellom tre ferdigdefinerte risikoprofiler for 100 000 NOK startkapital:

* **Konservativ Profil:** 5 posisjoner (20 % per posisjon), maks 1 per sektor, 5 % kontantbuffer, stram stop-loss på $1.5 \times \text{ATR}(14)$, holdeperiode 5–7 dager, og circuit breaker ved 3 tap på rad eller 8 % drawdown.
* **Balansert Profil (Anbefalt Standard):** 4 posisjoner (25 % per posisjon), maks 2 per sektor, 2 % kontantbuffer, stop-loss på $2.0\text{–}2.5 \times \text{ATR}(14)$, holdeperiode 10–20 dager, og circuit breaker ved 4 tap på rad eller 15 % drawdown.
* **Aggressiv Profil:** 3 posisjoner (33 % per posisjon), maks 2 per sektor, 1 % kontantbuffer, romslig stop-loss på $2.5 \times \text{ATR}(14)$, holdeperiode 20–30 dager, og circuit breaker ved 5 tap på rad eller 25 % drawdown.

---

## 5. Feilsøking (Troubleshooting)

* **«Missing ScriptRunContext» ved kjøring av `app.py`:**
  * Løst: [app.py](file:///c:/Users/frank/Desktop/AI%20trader/app.py) har nå en innebygd bootstrapper som automatisk starter `python -m streamlit run app.py` dersom du kjører filen direkte med `python app.py`.
* **DuckDB `IOException: file is being used by another process`:**
  * Løst: Alle moduler bruker nå `connect_duckdb()` i [src/db/duckdb_store.py](file:///c:/Users/frank/Desktop/AI%20trader/src/db/duckdb_store.py) som bruker `read_only=True` for spørringer og har automatisk retry-backoff ved samtidige prosesskall på Windows.
* **Lokal Ollama eller Gemini svarer ikke under `run_daily_scan.py`:**
  * [src/hitl/llm_validator.py](file:///c:/Users/frank/Desktop/AI%20trader/src/hitl/llm_validator.py) bruker nå **fail-closed**: signalet settes til `NO-GO` dersom ingen godkjent risikovurdering kan hentes. 3-dagers earnings guardrail blokkerer uavhengig av LLM-status.
