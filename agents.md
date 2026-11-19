# Multi-Agent Meglerbord-Arkitektur (OBX 15)

For dette prosjektet er systemet strukturert som et spesialisert meglerbord (**5-agents arkitektur**) fremfor å la én enkelt modell gjøre alt.

Kvantitative beregninger (indikatorer, MiFID II tick-size, kurtasje og porteføljeregler) kjøres alltid **100 % deterministisk** i ren Python og `DuckDB`, mens kunstig intelligens (lokal Ollama / Gemini 2.5 Flash) settes inn som en kvalitativ risikokontrollør der den faktisk har en *edge*:

$$\text{1. Kvantagent (Tall)} \xrightarrow{\text{Signaler}} \text{2. Nyhetsagent (Kontekst)} \xrightarrow{\text{Prompt}} \text{3. AI-Risikomanager (Vurdering)} \xrightarrow{\text{Telegram}} \text{Bruker (HITL)} \xrightarrow{\text{Godkjenn}} \text{4. Eksekverer (Saxo)} \xrightarrow{\text{Logg}} \text{5. Auditor (DuckDB)}$$

---

## 1. Kvant- og Signalagenten (*The Quantitative Analyst*)
* **Type:** Regelbasert Python- og SQL-motor (`src/signals/`).
* **Hovedoppgave:**
  * Skanner de 15 OBX-aksjene daglig ved børsslutt mot strategimatrisen.
  * Beregner endogene signaler (`RSI(2)`, 20-dagers `Breakout`, `Bollinger Squeeze`) på utbyttejusterte kurser (`adj_close`).
  * Sjekker eksogene filtre dersom strategien krever det (`OBX > SMA200`, `VIX < 25` med energisektor-override, og `Brent Crude` 2-dagers endring for energisektoren).
  * Genererer rå, ufiltrerte kjøps- og salgskandidater med referansekurser (`raw_close`), deterministisk `priority_rank` og ATR-baserte stop-loss-nivåer.

## 2. Nyhets- og Makroagenten (*The Intelligence / Data Scraper*)
* **Type:** Bakgrunnsarbeider (`src/data_ingestion/`).
* **Hovedoppgave:**
  * Lytter kontinuerlig mot **NewsWeb RSS** og **Euronext Live Notices** for offisielle selskapsmeldinger på de 15 OBX-aksjene.
  * Henter supplerende finans- og makronyheter (E24, Norges Bank) og sjekker kvartalskalenderen (`corporate_events`).
  * Pakker nyhetsbildet for de siste **48–72 timene** per aksje inn i et strukturert tekstformat klart for AI-risikovurdering.

## 3. AI-Risikomanager & «Djevelens Advokat» (*The Risk Officer*)
* **Type:** Hybrid LLM-agent – Lokal **Ollama** som standard + **Gemini 2.5 Flash** med strukturert JSON-skjema (`src/hitl/llm_validator.py`).
* **Hovedoppgave:**
  * Mottar kandidatene fra Kvantagenten sammen med nyhetspakken fra Nyhetsagenten.
  * Søker spesifikt etter selskapsspesifikk halerisiko og feller som tekniske grafer ikke ser:
    * **Earnings Guardrail (`EARNINGS_SOON`):** Er det kvartalstall innen 3 handelsdager (overnight gap risk)?
    * **Selskapsspesifikke sjokk (`COMPANY_SHOCK`):** Sanksjoner, kontraktskanselleringer, rettstvister, nedskrivninger eller emisjonsvarsler.
  * Returnerer et strukturert beslutningskort med `approve_recommendation`, `rejection_category` (`NONE`, `EARNINGS_SOON`, `COMPANY_SHOCK`, `MACRO_NOISE`), `bull_case`, `devils_advocate` og `risk_score`.
  * Logger **alle** vurderinger i `DuckDB` (`signal_evaluations`), uansett om den anbefaler kjøp (`GO`) eller avslag (`NO-GO`), for å muliggjøre eksakt **Shadow Tracking**.

## 4. Eksekverings- og Porteføljeagenten (*The Broker & Execution Trader*)
* **Type:** Python-tilstandsmaskin + `SQLite WAL` + Telegram HITL + Saxo OpenAPI v2 (`src/portfolio/`, `src/hitl/order_queue.py`, `src/brokers/`).
* **Hovedoppgave:**
  * **Porteføljekontroll:** Håndterer kontantbalanse, sikrer maks 4 samtidige posisjoner (25 % allokering) og håndhever sektorbegrensningen (maks 2 per sektor).
  * **Human-in-the-Loop:** Sender interaktive kjøpsforslag (`BUY`) til Telegram med ferdig utregnet innsats, stop-loss og AI-begrunnelse, og venter på brukergodkjenning innen **14 timer** (mens `SELL` / stop-loss eksekveres automatisk for å beskytte nedside).
  * **Ordreutførelse (Saxo OpenAPI):**
    * Avrunder limit-kursen til korrekt MiFID II tick-size på Oslo Børs.
    * Kontrollerer at åpningskursen ikke har løpt fra signalet (`+1.5 %` / `-2.0 %` Gap Guardrail).
    * Sikrer idempotens og tildeler `x-request-id` for å forhindre duplikathandler innenfor 15-sekundersvinduet.
    * Håndterer automatisk fornying av OAuth-tokens og rate limits (maks 1 ordre/sekund).

## 5. Attribusjons- og Revisjonsagenten (*The Performance Auditor*)
* **Type:** Analytisk SQL- og regresjonsagent i `DuckDB` / Python (`src/analytics/`).
* **Hovedoppgave:**
  * Kjører hver søndag kveld mot `obx_analytics.duckdb`:
    * **Faktordekomponering & Regresjon:** Deler realisert avkastning inn i *Markedsbidrag (OBX)*, *Sektorbidrag* og *Ren Strategi-Alfa*, samt kjører multippel faktorregresjon mot $\beta_{\text{OBX}}$, $\beta_{\text{Brent}}$ og $\beta_{\text{FX}}$ ($\text{USDNOK}$).
    * **Cost Drag-analyse:** Regner ut nøyaktig hvor stor andel av bruttoavkastningen som ble spist opp av kurtasje og bid-ask spread (mål: $< 20 \%$).
    * **Shadow Tracking (A/B-revisjon):** Sammenligner **ALLE signaler (Kohort A)**, **AI GO (Kohort B)** og **AI NO-GO (Kohort C)** – brutt ned på avslagsårsak (`EARNINGS_SOON` vs. `COMPANY_SHOCK` vs. `MACRO_NOISE`).
    * Varsler på Telegram om AI-filteret faktisk øker Sharpe/Sortino Ratio, eller om systemprompten er i ferd med å overfiltrere gode handler pga. makrostøy.
