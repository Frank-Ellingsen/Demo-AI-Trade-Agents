# Antigravity Agent Spec: AI-Drevet Kvantitativ Swing-Trading på OBX

## 1. Mål og Overordnet Filosofi
Formålet med prosjektet er å etablere en robust, systematisk swing-trading-motor i Python strukturert som et **spesialisert meglerbord (5-agents arkitektur)** med en **Human-in-the-Loop (HITL)**-godkjenningssløyfe.

Grunnprinsippet er streng **ansvarsdeling (Separation of Concerns)**:
* **Deterministisk Kvant & Eksekvering:** Matematiske beregninger (indikatorer, posisjonsstørrelse, MiFID II tick-size, kurtasje og porteføljeregler) kjøres alltid 100 % deterministisk i ren Python og `DuckDB`. Ingen ordrehåndtering eller utregning kan feile på grunn av AI-hallusinasjoner.
* **Kvalitativ AI-Risikokontroll:** Språkmodellen (lokal Ollama / Gemini 2.5 Flash) settes kun inn der den har en reell *edge* – som en kritisk risikomanager og «Djevelens Advokat» som gransker børsmeldinger, kvartalskalender og halerisiko før ordren sendes til Telegram for godkjenning.

> **Kritiske målparametre (Stage-Gate Baseline):**
> * **Sortino Ratio:** $> 1.2$
> * **Sharpe Ratio:** $> 0.9$
> * **Maksimal Drawdown:** $< 18 \%$
> * **Cost Drag Constraint:** Samlet kurtasje, depotgebyr og spread/slippage-kostnader skal holdes under $20 \%$ av brutto realisert gevinst. (Ved ~0.26 % – 0.40 % round-trip friksjon krever dette en gjennomsnittlig bruttogevinst per handel på $> 1.3 \% - 2.0 \%$).

---

## 2. Multi-Agent Meglerbord-Arkitektur (De 5 Spesialistagentene)

$$\text{1. Kvantagent (Tall)} \xrightarrow{\text{Signaler}} \text{2. Nyhetsagent (Kontekst)} \xrightarrow{\text{Prompt}} \text{3. AI-Risikomanager (Vurdering)} \xrightarrow{\text{Telegram}} \text{Bruker (HITL)} \xrightarrow{\text{Godkjenn}} \text{4. Eksekverer (Saxo)} \xrightarrow{\text{Logg}} \text{5. Auditor (DuckDB)}$$

### Agent 1: Kvant- og Signalagenten (*The Quantitative Analyst*)
* **Type:** Regelbasert Python-motor (`DuckDB` SQL / Python).
* **Hovedoppgave:**
  * Skanner de 15 OBX-aksjene daglig ved børsslutt (kl. 16:40 CET) mot strategimatrisen.
  * Beregner endogene signaler (`RSI(2)`, 20-dagers `Breakout`, `Bollinger Squeeze`) på **utbyttejusterte kurser (`adj_close`)**.
  * Sjekker eksogene filtre (`OBX > SMA(200)`, `VIX < 25` med energi/shipping-unntak, og 2-dagers `Brent Crude`-endring for energisektoren).
  * Genererer rå, ufiltrerte kjøps- og salgskandidater med referansekurser (`raw_close`), `priority_rank` og ATR-baserte stop-loss-nivåer.

### Agent 2: Nyhets- og Makroagenten (*The Intelligence / Data Scraper*)
* **Type:** Bakgrunnsarbeider (`urllib` / `feedparser` / `BeautifulSoup`).
* **Hovedoppgave:**
  * Lytter kontinuerlig mot **NewsWeb RSS** og **Euronext Live Notices** for offisielle selskapsmeldinger (innsidehandel, resultatvarsler, kontrakter, emisjoner) på de 15 OBX-aksjene.
  * Henter supplerende finans- og makronyheter (E24, Norges Bank) og sjekker kvartalskalenderen (`corporate_events`).
  * Pakker nyhetsbildet for de siste **48–72 timene** per aksje inn i et strukturert tekstformat klart for AI-risikovurdering.

### Agent 3: AI-Risikomanager & «Djevelens Advokat» (*The Risk Officer*)
* **Type:** Hybrid LLM-agent (Lokal **Ollama** som standard for 0 kr marginalkostnad + **Gemini 2.5 Flash** med strukturert JSON-skjema).
* **Hovedoppgave:**
  * Mottar kandidatene fra Kvantagenten sammen med 72-timers nyhetspakke fra Nyhetsagenten.
  * Søker spesifikt etter halerisiko og kvalitative feller som tekniske grafer ikke fanger opp:
    * **Earnings Guardrail:** Er det kvartalstall innen 3 handelsdager (overnight gap risk)?
    * **Selskapsspesifikke sjokk:** Sanksjoner, kontraktskanselleringer, rettstvister, nedskrivninger eller emisjonsvarsler.
  * Returnerer et strengt validert beslutningskort med `approve_recommendation` (`bool`), `bull_case`, `devils_advocate`, `risk_score` (`LOW`, `MEDIUM`, `HIGH`) og `earnings_blackout_flag`.
  * Logger **alle** vurderinger i `DuckDB` (`signal_evaluations`), uansett om den anbefaler kjøp (`GO`) eller avslag (`NO-GO`), for å muliggjøre eksakt **Shadow Tracking**.

### Agent 4: Eksekverings- og Porteføljeagenten (*The Broker & Execution Trader*)
* **Type:** Deterministisk Python-tilstandsmaskin + `SQLite WAL` + Telegram HITL + Saxo OpenAPI v2.
* **Hovedoppgave:**
  * **Porteføljekontroll:** Håndterer kontantbalanse, sikrer maks 4 samtidige posisjoner (25 % per posisjon), håndhever sektorbegrensningen (maks 2 per sektor), og bruker deterministisk `priority_rank` ved flere samtidige signaler.
  * **Human-in-the-Loop (Telegram):**
    * *Innganger (`BUY`):* Sender interaktive forslag til Telegram med ferdig utregnet innsats, stop-loss og AI-begrunnelse, og venter på brukergodkjenning innen **14 timer** (ellers `EXPIRED`).
    * *Utganger (`SELL` / Stop-Loss):* Eksekveres automatisk uten å blokkeres av manuell godkjenning for å beskytte nedside, med umiddelbart Telegram-varsel.
  * **Ordreutførelse (Saxo OpenAPI v2):**
    * Avrunder limit-kursen til korrekt MiFID II tick-size på Oslo Børs.
    * **Åpningsgap-beskyttelse (09:00 CET Guardrail):** Kontrollerer at markedskursen ved åpning (`Open T+1`) ikke har løpt fra signalet ($> +1.5 \%$ over eller $< -2.0 \%$ under `Close T`).
    * Sikrer idempotens og tildeler unik `x-request-id` for å forhindre duplikathandler innenfor 15-sekundersvinduet.
    * Håndterer automatisk fornying av OAuth-tokens, pre-market sesjonssjekk kl. 08:15 CET og rate limits (maks 1 ordre/sekund, 120 req/min).

### Agent 5: Attribusjons- og Revisjonsagenten (*The Performance Auditor*)
* **Type:** Analytisk SQL-agent (`DuckDB` + automatisert ukentlig revisjonsjobb).
* **Hovedoppgave:**
  * Kjører hver søndag kveld mot `obx_analytics.duckdb`:
    * **Faktordekomponering:** Deler realisert avkastning inn i *Markedsbidrag (OBX)*, *Sektorbidrag* og *Ren Strategi-Alfa*.
    * **Cost Drag-analyse:** Regner ut nøyaktig hvor stor andel av bruttoavkastningen som ble spist opp av kurtasje og bid-ask spread/slippage (mål: $< 20 \%$).
    * **Shadow Tracking (A/B-revisjon):** Sammenligner **Kohort A (Alle signaler)**, **Kohort B (AI GO)** og **Kohort C (AI NO-GO)**.
  * Varsler på Telegram om AI-filteret faktisk øker Sharpe/Sortino Ratio og reduserer Max Drawdown, eller om systemprompten overfiltrerer gode vinnerhandler.

---

## 3. Investeringsunivers & Meglerarkitektur

### 3.1 Investeringsunivers (OBX 15)
For å eliminere likviditetsrisiko, sikre stram bid-ask spread og unngå unødvendig slippage, er universet låst til de 15 mest likvide aksjene på Oslo Børs (XOSL):

| Sektor | Ticker | Selskap | Sektorindeks | Likviditetsbånd | Likviditetsprofil |
| :--- | :--- | :--- | :--- | :---: | :--- |
| **Energi / Oljeservice** | `EQNR.OL` | Equinor | `OBEP.OL` | 6 | Høyeste omsetning på OBX, global energi |
| | `AKRBP.OL` | Aker BP | `OBEP.OL` | 5 | Ren E&P-eksponering mot norsk sokkel |
| | `SUBC.OL` | Subsea 7 | `OBEP.OL` | 5 | Subsea ingeniørtjenester og oljeservice |
| **Shipping** | `FRO.OL` | Frontline | `OBSH.OL` | 5 | Tankfart, råoljefrakt og rater |
| **Finans** | `DNB.OL` | DNB Bank | `OBFI.OL` | 6 | Norges største bank, rentefølsomhet |
| | `STB.OL` | Storebrand | `OBFI.OL` | 5 | Livsforsikring og kapitalforvaltning |
| | `GJFG.OL` | Gjensidige Forsikring | `OBFI.OL` | 5 | Skadeforsikring, lav beta og defensiv profil |
| **Industri / Materialer** | `NHY.OL` | Norsk Hydro | `OBMA.OL` | 6 | Global aluminiumsetterspørsel og energipriser |
| | `YAR.OL` | Yara International | `OBMA.OL` | 5 | Gjødsel, ammoniakk og naturgasspriser |
| | `KOG.OL` | Kongsberg Gruppen | `OBIN.OL` | 5 | Forsvar, maritim teknologi og ordrereserve |
| **Sjømat** | `MOWI.OL` | Mowi | `OBSE.OL` | 5 | Verdensledende lakseoppdrett |
| | `SALM.OL` | SalMar | `OBSE.OL` | 5 | Kostnadseffektiv oppdrett og biologisk syklus |
| **Telekom / Konsum / Tech**| `TEL.OL` | Telenor | `OBTE.OL` | 5 | Defensiv telekom, stabilt utbytte |
| | `ORK.OL` | Orkla | `OBCS.OL` | 5 | Merkevarer til forbruker, lav konjunkturfølsomhet |
| | `NOD.OL` | Nordic Semiconductor | `OBIT.OL` | 5 | Halvledere, Bluetooth Low Energy, syklisk vekst |

### 3.2 Meglerfriksjon & Skatteminimering

```
                     +-----------------------------------+
                     |      Megler- & Skattemodell       |
                     +-----------------+-----------------+
                                       |
          +----------------------------+----------------------------+
          |                                                         |
          v                                                         v
+-------------------+                                     +-------------------+
|     Saxo Bank     |                                     |      Nordnet      |
| - OpenAPI v2      |                                     | - Nordnet Mini    |
| - 0.08 % (min 10) |                                     | - 0.15 % (min 29) |
| - Depot: 0 % ASK* |                                     | - 0 kr depotgebyr |
+-------------------+                                     +-------------------+
          |                                                         |
          +----------------------------+----------------------------+
                                       |
                                       v
                    +-------------------------------------+
                    |       Felles Friksjonsvern          |
                    | - Aksjesparekonto (ASK): Utsatt skatt|
                    | - Valutadrag: 0 % (NOK-oppgjør)     |
                    +-------------------------------------+
```

1. **Saxo Bank (OpenAPI):**
   * **Kurtasje:** 0.08 % (minimum 10 NOK) for Classic Tier (20 NOK per vei ved 25 000 NOK posisjon).
   * **Depotgebyr (Custody Fee):** 0.00 % på norsk ASK (konfigurerbart opp til 0.12 % p.a. i `brokers.yaml` for stresstesting).
   * **Teknisk protokoll:** REST API v2 (`/trade/v2/orders`) med `x-request-id` (15s idempotensvindu) og WebSocket med Session ContextId.
   * **Rate Limits:** 120 forespørsler/minutt, maks 1 ordre/sekund per konto.
2. **Nordnet (Nordnet Mini):**
   * **Kurtasje:** 0.15 % (minimum 29 NOK) – 37,50 NOK per vei ved 25 000 NOK posisjon.
   * **Depotgebyr:** 0 kr på nordiske aksjer.
3. **Skattemodell (Aksjesparekonto - ASK):**
   * Posisjoner handles via ASK for å sikre utsatt beskatning ($37.84 \%$ belastes først ved uttak over innskutt kostpris).
   * Ingen valutafriksjonskostnader (0 % FX-drag) ettersom alle posisjoner gjøres opp i NOK.

---

## 4. Strategiarkitektur og Eksperimentmatrise

### 4.1 Holdeperioder
* **Kort swing (1–10 dager):** Rask reversering etter oversolgte nivåer eller momentumimpulser.
* **Medium swing (1–20 dager):** Standard tidsvindu tilpasset OBX-aksjenes typiske korreksjons- og trendbølger.
* **Lang swing (1–30 dager):** Trendfølgende posisjoner med trailing exit for industrielle og råvaredrevne sykluser.

### 4.2 Signalgenerering: Endogen vs. Eksogen

#### Endogene Signaler (Beregnet på `adj_close`)
1. **RSI Mean Reversion:**
   * **Kjøp:** `adj_close > SMA(200)` og `RSI(2) < 10`.
   * **Salg:** `adj_close > SMA(5)`, katastrofestopp ($3.0 \times \text{ATR}(14)$) eller nådd tidsgrense.
   * **Tie-breaker:** Lavest `RSI(2)`.
2. **Trend Breakout & Volumekspansjon:**
   * **Kjøp:** `adj_close > High(20)` og `Volume > 1.3 * SMA(Volume, 20)`.
   * **Salg:** `adj_close < Low(10)`, $2.5 \times \text{ATR}(14)$ trailing stop eller nådd tidsgrense.
   * **Tie-breaker:** Høyest relativt volum (`Volume / SMA(Volume, 20)`).
3. **Bollinger Band Squeeze:**
   * **Kjøp:** Båndbredden (`(Upper - Lower) / Middle`) på 60-dagers bunnivå og `adj_close > Upper`.
   * **Salg:** `adj_close < SMA(20)` (midtre bånd), $2.0 \times \text{ATR}(14)$ trailing stop eller nådd tidsgrense.
   * **Tie-breaker:** Høyest relativt volum.

#### Eksogene Signaler (Markeds- og Råvareregimer)
1. **Markedsregimefilter (OBX & VIX):** `OBX > SMA(200)` og `VIX < 25` (unntak for Energi/Shipping dersom `Brent_2d_change > +1.5 %`).
2. **Råvarekobling for energisektoren:** `EQNR`, `AKRBP` og `SUBC` krever `Brent_2d_change > -2.5 %`.
3. **Sektormomentum:** Sektorens indeks må ha positivt 10-dagers relativt momentum mot `OBX`.

---

## 5. Systemarkitektur & Databaseinfrastruktur

Systemet benytter to lokale databaser tilpasset sine respektive styrker:

```
+-------------------------------------------------------------+
|            Python Multi-Agent Trading Engine                |
+------------------------------+------------------------------+
                               |
       +-----------------------+-----------------------+
       |                                               |
       v                                               v
+-------------------------------+   +---------------------------------+
| SQLite (hitl_orders.db)       |   | DuckDB (obx_analytics.duckdb)  |
| - Modus: PRAGMA journal=WAL   |   | - ohlcv_daily (raw + adj)       |
| - Tabell: pending_orders      |   | - newsweb_messages (72t)        |
| - Agent 4 tilstandsmaskin     |   | - signal_evaluations (Agent 3)  |
| - Telegram-ID & x-request-id  |   | - trade_ledger (Agent 5 Audit)  |
+-------------------------------+   +---------------------------------+
```

### 5.1 Tabellskjema for SQLite (`hitl_orders.db` – Agent 4 Ordrekø)
```sql
CREATE TABLE IF NOT EXISTS pending_orders (
    order_id TEXT PRIMARY KEY,
    x_request_id TEXT UNIQUE NOT NULL,  -- 15s idempotensnøkkel mot Saxo OpenAPI
    ticker TEXT NOT NULL,
    action TEXT NOT NULL,               -- 'BUY' eller 'SELL'
    order_type TEXT DEFAULT 'LIMIT',    -- 'LIMIT', 'MOO_GUARDED', 'STOP_EXIT'
    shares INTEGER NOT NULL,
    signal_close_price REAL NOT NULL,   -- Close(T) referansekurs
    limit_price REAL NOT NULL,          -- MiFID II tick-size avrundet kurs
    stop_loss_price REAL,               -- ATR-basert stop-loss
    max_gap_up_pct REAL DEFAULT 0.015,  -- +1.5% maks åpningsgap
    max_gap_down_pct REAL DEFAULT -0.02,-- -2.0% maks åpningsgap
    strategy_name TEXT NOT NULL,
    horizon_days INTEGER NOT NULL,
    priority_rank REAL,
    llm_provider TEXT,                  -- 'ollama' eller 'gemini'
    llm_recommendation TEXT,            -- 'GO' eller 'NO-GO'
    llm_confidence REAL,
    risk_score TEXT,                    -- 'LOW', 'MEDIUM', 'HIGH'
    earnings_blackout_flag INTEGER DEFAULT 0,
    bull_case TEXT,
    devils_advocate TEXT,
    requires_hitl INTEGER DEFAULT 1,    -- 1 for BUY (14t frist), 0 for auto SELL/Stop
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP,
    status TEXT DEFAULT 'PENDING',      -- 'PENDING', 'APPROVED', 'AUTO_APPROVED', 'REJECTED', 'EXPIRED', 'CANCELLED_GAP', 'EXECUTED'
    telegram_message_id INTEGER
);
```

### 5.2 Tabellskjema for DuckDB (`obx_analytics.duckdb` – Agent 1, 2, 3 og 5)
* `ohlcv_daily`: Historiske kurser (`raw_open`, `raw_high`, `raw_low`, `raw_close`, `adj_close`, `volume`, `turnover_nok`, `dividend_amount`).
* `macro_regime`: Eksogene tidsserier (`obx_close`, `obx_sma200`, `vix_close`, `brent_close`, `brent_2d_pct`, `usdnok`).
* `newsweb_messages`: 72-timers børsmeldingsarkiv fra Agent 2 (`message_id`, `ticker`, `published_at`, `category`, `title`, `summary`).
* `signal_evaluations`: **Alle** vurderinger fra Agent 3 (`eval_id`, `signal_date`, `ticker`, `strategy_name`, `llm_provider`, `approve_recommendation`, `risk_score`, `earnings_blackout_flag`, `bull_case`, `devils_advocate`), uavhengig av om signalet ble godkjent eller avslått.
* `trade_ledger`: Komplett reskontro for Agent 5 (Attribusjon, Cost Drag % og Shadow Tracking for `COHORT_A_BASE`, `COHORT_B_LLM_GO` og `COHORT_C_LLM_NOGO`).

---

## 6. Prosjektstruktur

```text
AI trader/
├── antigravity-project-spec.md           # Dette spesifikasjonsdokumentet
├── config/                               # Konfigurasjon og parametere
│   ├── settings.py                       # Innstillinger (miljøvariabler, paths, LLM-valg)
│   ├── universe.yaml                     # De 15 OBX-aksjene, sektorer, ISIN og likviditetsbånd
│   ├── strategies.yaml                   # Parametere for 1-10d, 1-20d, 1-30d, RSI, Breakout, Bollinger
│   └── brokers.yaml                      # Gebyrstrukturer (Saxo vs. Nordnet), ASK-regler, Gap Guardrails
│
├── data/                                 # Lokale databaser og mellomlagring (gitignorerte)
│   ├── obx_analytics.duckdb              # DuckDB: OHLCV, nyhetsarkiv, signal_evaluations, trade_ledger
│   ├── hitl_orders.db                    # SQLite (WAL-modus): Aktive Telegram-ordrer og status
│   └── raw_news/                         # Mellomlagrede RSS/NewsWeb-meldinger (JSON)
│
├── src/                                  # Hovedkildekode delt inn etter de 5 agentene
│   ├── __init__.py
│   ├── db/                               # Delt databaseinfrastruktur
│   │   ├── sqlite_store.py               # SQLite WAL-tilkobling og ordrekø
│   │   └── duckdb_store.py               # DuckDB skjemaoppsett (inkl. signal_evaluations)
│   │
│   ├── data_ingestion/                   # Agent 2: Nyhets- og Makroagenten (The Intelligence Scraper)
│   │   ├── ohlcv_loader.py               # Henter historiske dagsdata (raw + dividend-adjusted)
│   │   ├── newsweb_rss.py                # Lytter mot NewsWeb RSS og pakker 48-72t kontekst
│   │   └── corporate_actions.py          # Kvartalskalender (3-dagers Earnings Guardrail)
│   │
│   ├── signals/                          # Agent 1: Kvant- og Signalagenten (The Quantitative Analyst)
│   │   ├── endogenous.py                 # RSI(2), 20d Breakout, Bollinger Squeeze
│   │   ├── exogenous.py                  # OBX-regime (SMA200), VIX, Brent-oljefilter
│   │   └── factory.py                    # Genererer rå signaler med ATR-stops og priority_rank
│   │
│   ├── hitl/                             # Agent 3 & 4: AI-Risikomanager & Telegram HITL
│   │   ├── llm_validator.py              # Agent 3: «Djevelens Advokat» (Ollama/Gemini -> signal_evaluations)
│   │   ├── telegram_bot.py               # Agent 4: Interaktive Telegram-kort med [Godkjenn] / [Avvis]
│   │   └── order_queue.py                # Agent 4: SQLite WAL-kø, x-request-id og 14t auto-expiry
│   │
│   ├── portfolio/                        # Agent 4: Porteføljeregler og friksjonsmotor
│   │   ├── allocator.py                  # Posisjonsstørrelse (maks 4 pos, 2/sektor, tie-breaker)
│   │   ├── risk_manager.py               # 09:00 Gap Guardrail (+1.5% / -2.0%) og ATR stop-loss
│   │   └── cost_tax_model.py             # Kurtasje, Cost Drag %, MiFID II tick-size og ASK
│   │
│   ├── brokers/                          # Agent 4: Meglerintegrasjon (Saxo OpenAPI v2)
│   │   ├── base.py                       # Abstrakt Broker-grensesnitt
│   │   ├── saxo_client.py                # OAuth PKCE, token refresh og rate limits (1 ordre/sek)
│   │   ├── saxo_execution.py             # Ordreplassering med tick-size og 15s idempotens (x-request-id)
│   │   └── nordnet_simulator.py          # Papirhandelsimulator med Nordnet Mini-avgifter
│   │
│   ├── backtest/                         # Historisk simulering og Walk-Forward
│   │   ├── engine.py                     # Hendelsesdrevet simulator
│   │   ├── walk_forward.py               # Rullerende trening, Out-of-Sample og YTD-test
│   │   └── metrics.py                    # Sharpe, Sortino, Calmar, Max Drawdown, Cost Drag Ratio
│   │
│   └── analytics/                        # Agent 5: Attribusjons- og Revisjonsagenten (The Auditor)
│       ├── attribution.py                # Dekomponerer: Marked (OBX) + Sektor + Ren Alfa
│       ├── shadow_tracker.py             # A/B-revisjon: ALLE signaler vs. AI GO vs. AI NO-GO
│       └── reports.py                    # Søndagsrapport (Tufte-formatert) + Telegram-varsel
│
├── scripts/                              # Operasjonelle kjøreskript for agentene
│   ├── init_databases.py                 # Oppretter og verifiserer SQLite WAL og DuckDB tabeller
│   ├── run_daily_scan.py                 # Kjører pipeline: Agent 1 -> Agent 2 -> Agent 3 -> Agent 4
│   ├── run_weekly_audit.py               # Kjører Agent 5 hver søndag kveld mot DuckDB
│   ├── run_backtest_matrix.py            # Kjører full testmatrise (1-10d, 1-20d, 1-30d, YTD)
│   └── check_saxo_session.py             # Pre-market sjekk kl 08:15 CET av OAuth-token
│
├── tests/                                # Automatiserte enhetstester
│   ├── test_signals.py                   # Tester Agent 1 signalberegninger
│   ├── test_cost_model.py                # Tester Saxo/Nordnet-kurtasje og 20% Cost Drag gate
│   ├── test_tick_size.py                 # Tester avrunding mot Oslo Børs MiFID II tick-regler
│   └── test_hitl_queue.py                # Tester Agent 4 SQLite WAL, x-request-id og 14t auto-expiry
│
├── .env.example                          # Mal for nøkler
├── pyproject.toml                        # Prosjektmetadata
└── README.md                             # Prosjektoversikt og hurtigstart
```
