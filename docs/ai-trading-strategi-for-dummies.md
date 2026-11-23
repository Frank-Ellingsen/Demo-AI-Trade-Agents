# AI Trading Strategi for Dummies

> **⚠️ VIKTIG ANSVARSFRASKRIVELSE & RISIKOADVARSEL (DEMO / PAPER TRADING)**  
> Dette systemet og dokumentet er utelukkende laget for **demonstrasjon, læring, kvantitativ forskning og risikofri papirhandel (Paper Trading)**.  
> * Innholdet utgjør **ikke** finansiell rådgivning, investeringsrådgivning eller en anbefaling om å kjøpe eller selge verdipapirer.  
> * Historiske simuleringer (*backtests*) og fiktive resultater er **ingen garanti for fremtidig avkastning**.  
> * All handel i verdipapirer med ekte penger innebærer risiko for betydelig tap — du er selv 100 % ansvarlig for alle egne investeringsbeslutninger.

Å bygge en automatisert tradingstrategi som kombinerer kvantitative metoder med kunstig intelligens handler verken om ufeilbarlige spådommer eller lynrask datatrafikk på millisekundnivå. Tvert imot bygger vellykket swing-trading på disiplinert kapitalbevaring, matematisk risikostyring og kontroll over friksjonskostnader. Mange nybegynnere taper penger fordi de undervurderer effekten av spread, kurtasje og manglende stop-loss, eller fordi de handler illikvide aksjer uten sikkerhetsmargin.

Dette dokumentet gir en praktisk og lettfattelig innføring i hvordan **AI Trading Desk** er bygget opp, hvordan du velger riktig risikoprofil for **Alpaca Paper Trading**, hvorfor likviditet og meglernative OCO Bracket-ordrer er avgjørende, og hvordan du evaluerer strategien uke for uke.

---

## 1. Hva AI-Trading Faktisk Er (og Hva Det Ikke Er)

Den vanligste misforståelsen i moderne finans er at en språkmodell (LLM) kan «spå» morgendagens aksjekurs. Å be en AI om å «finne aksjer som skal stige» gir tilfeldige resultater og falsk trygghet.

I **AI Trading Desk** brukes ikke AI som spåmann, men som et spesialisert kontrollteam der **deterministisk matematikk** og **kvalitativ risikokontroll** er strengt adskilt:

1. **Agent 1 — Kvantitativ Signalgiver (Tall, ikke synsing):** En regelmotor i Python som skanner likvide aksjer (S&P 500 / ETF / OBX) etter statistiske mønstre: `P > SMA(200)`, 12–1 måneders momentum, `RSI(2)` og volumkrav.
2. **Agent 2 — Hendelses- og Nyhetsvakt:** Overvåker ferske selskapsmeldinger og kvartalskalendere. Dersom et selskap legger frem kvartalstall innen **48 timer / 3 handelsdager**, blokkeres handelen automatisk (*Earnings Trap Protection*) for å unngå plutselige kursfall over natten.
3. **Agent 3 — AI-Risikomanager («Djevelens Advokat»):** Språkmodellen (lokal **Ollama** med **Gemini** som fallback) vurderer først etter at matematikken har sagt ja. Den leter etter feller som grafene ikke ser (rettstvister, emisjoner, kontraktskanselleringer) og er **fail-closed** — svarer ikke modellen, blir svaret automatisk `NO-GO`.
4. **Agent 4 — 3-Trinns Human-in-the-Loop (HITL) & OCO-Eksekvering:**
   * **Trinn 1 (Telegram):** Du mottar et ferdig ordrekort på mobilen med antall aksjer, limit-kurs, Stop-Loss og Take-Profit. Ingen ordre sendes før du trykker **✅ Godkjenn**.
   * **Trinn 2 (E-post Failover):** Hvis Telegram er nede, sender `failover_approver.py` en e-post med sikre `/approve`- og `/reject`-lenker.
   * **Trinn 3 (Web Terminal):** Du kan alltid godkjenne eller avvise direkte i nettleseren (`http://localhost:8000/`).
   * Når du godkjenner, sendes en **meglernativ OCO Bracket-ordre** til **Alpaca Paper API v2** — det betyr at Stop-Loss og Take-Profit ligger ferdig låst hos megleren i samme sekund som kjøpet går gjennom.
5. **Agent 5 — Ukentlig Revisor (DuckDB):** Måler Sortino Ratio, konseptdrift og **Shadow Tracking** (sammenligner handler AI-en godkjente mot handler den avviste).

> **Kjerneprinsipp:** Matematikken finner kandidatene, kalendervakten stopper kvartalsfeller, AI-en spiller djevelens advokat, og **du** trykker på godkjenningsknappen.

---

## 2. Tre Ferdige Risikoprofiler (Alpaca Paper / `$100 000` USD)

Når du starter med **Alpaca Paper Trading** (eller velger et eget beløp som `$50 000` USD i `index.html`), kan du velge mellom tre ferdigkalibrerte risikoprofiler i `dynamic_recommender.py`:

| Parameter | 1. Konservativ (Index Core) | 2. Balansert (Multi-Sektor) | 3. Aggressiv (AI & Tech) |
| :--- | :--- | :--- | :--- |
| **Aksjer / ETF-er** | `SPY, QQQ, XLV, IWM` | `AAPL, MSFT, JPM, CAT, XOM` | `NVDA, AVGO, TSLA, META, PLTR, AMZN` |
| **Kontantbuffer** | 20 % kontanter | 10 % kontanter | 5 % kontanter |
| **Stop-Loss (ATR-basert)** | `1.2 × ATR(14)` (stram) | `1.8 × ATR(14)` (balansert) | `2.5 × ATR(14)` (romslig) |
| **Målkurs (Reward / Risk)** | `2.0 : 1` | `2.2 : 1` | `2.5 : 1` |
| **Circuit Breaker (MaxDD)** | `-4.0 %` | `-6.0 %` | `-8.0 %` |
| **Passer best for** | Lav risiko, jevn kurve | Standard for de fleste | Høyere vekst og større svingninger |

### Hvorfor bruker vi ATR(14) til Stop-Loss i stedet for fast prosent?
En rolig indeks-ETF som `SPY` svinger kanskje 0,8 % om dagen, mens en AI-aksje som `NVDA` eller `TSLA` lett kan svinge 3–4 % på en helt vanlig dag. Setter du en fast 2 % stop-loss på `TSLA`, blir du slått ut av normal dagsstøy (*whipsaw*) før oppturen starter. **ATR(14)** (*Average True Range*) måler hvor mye aksjen faktisk svinger per dag, slik at Stop-Loss automatisk tilpasses hver enkelt aksje.

---

## 3. Valg av Investeringsunivers: Hvorfor S&P 500 Slår OTC / Micro-Caps

I `backtest_us_multi_universe.py` sammenligner systemet fire strategier over 252 handelsdager:

1. **Liquid S&P Momentum (Vinneren):** Handler kun store, likvide S&P 500-aksjer (`> $50M` daglig volum) med `$0` kurtasje på Alpaca Paper og ATR Stop-Loss. Høyest risikojustert avkastning (Sortino) og lavest drawdown.
2. **AI Regime Switcher (VIX-styrt):** Roterer automatisk basert på fryktindeksen **VIX**:
   * `VIX < 18` (rolig oppgang): Kjører **Liquid S&P Momentum**.
   * `VIX 18–24` (urolig / sidelengs): Bytter til **Systematic Covered Calls (SPY)** for månedlig premieinntekt.
   * `VIX > 24` (markedspanikk): Går i **kontanter / T-Bills** for å beskytte kapitalen.
3. **Systematic Covered Calls (SPY):** Jevnere kurve og løpende premie (~1,35 %/mnd), men kapper oppsiden i kraftige børsrallyer.
4. **OTC & Micro-Caps (Advarsel):** Ser ofte spennende ut på papiret, men **1,5 %–3,0 % bid-ask spread** spiser opp avkastningen, og ved børsfall finnes det ingen kjøpere når stop-loss skal utløses. **Hold deg unna illikvide småaksjer.**

*(For norske aksjer støtter systemet også en nordisk referansemodell for **OBX 15** med 2-dagers Brent Crude-filter på Aksjesparekonto).*

---

## 4. Dekkede Opsjoner vs. Nakne Opsjoner (Kort Forklart)

* **Dekkede Kjøpsopsjoner (Covered Calls):** Du eier allerede 100 aksjer (f.eks. `SPY`) og selger en kjøpsopsjon mot en kontantpremie. Du har **ingen risiko for ubegrenset tap** og ingen marginrenter. Godt egnet i rolige eller sidelengs markeder.
* **Nakne Opsjoner (Naked Calls/Puts):** Du selger opsjoner *uten* å eie aksjene. Tapsrisikoen er teoretisk ubegrenset, og megleren kan tvangslukke kontoen din ved plutselige kurshopp. **Skal unngås helt i automatiske strategier.**

---

## 5. US Intradag-Strategier (`04 US Intraday` — Dagshandel uten Natt-Risiko)

Mens **03 Trading** driver *swing trading* (posisjoner som holdes over dager eller uker), handler **04 US Intraday** kun inne i den amerikanske børsdagen (NYSE/Nasdaq `09:30–16:00 EST` / `15:30–22:00 CET`). Alle intradagsposisjoner **tvangslukkes automatisk kl. 15:55 EST (21:55 CET)** (*EOD-flatting*), slik at du aldri sitter med risiko over natten.

### A. To Aksjestrategier på 5-Minutters Data (`us_intraday_agent.py`)
Agenten skanner de mest likvide amerikanske aksjene og ETF-ene (`SPY, QQQ, NVDA, AAPL, MSFT, META, AMZN, TSLA, AMD, AVGO` m.fl.) hvert minutt med to klassiske institusjonelle oppsett:

| Strategi | Hva den ser etter (Enkelt forklart) | Hvorfor den fungerer |
| :--- | :--- | :--- |
| **15-min ORB** (*Opening Range Breakout*) | Måler høyeste og laveste kurs de første 15 minuttene etter børsåpning (`15:30–15:45 CET`). Kjøper når kursen bryter over toppen med volum. | De første 15 minuttene samler nattens nyheter og institusjonelle ordrer. Et brudd ut av spennet viser hvem som vant kampen (kjøpere vs. selgere). |
| **VWAP Pullback** | **VWAP** er dagens volumvektede gjennomsnittskurs. Agenten kjøper når en aksje i opptrend faller kort tilbake til VWAP og snur opp igjen. | Store fond måles ofte mot VWAP. Det gir lav inngangsrisiko fordi Stop-Loss kan plasseres tett rett under VWAP. |

### B. Toveis Intradag Opsjonsagent — Både Bull & Bear (`us_intraday_options_agent.py`)
I motsetning til vanlige aksjekjøp som bare tjener penger i oppgangsmarked, analyserer den autonome opsjonsagenten live 5m-grafer og velger retning automatisk:
* **🟢 BUY CALL (Bullish):** Når kursen bryter *over* 15-min High og ligger over VWAP.
* **🔴 BUY PUT (Bearish):** Når kursen bryter *under* 15-min Low og ligger under VWAP — slik at strategien kan utnytte røde dager.
* **Streng sikkerhetsramme:** Hardt tak på **`$5 000` / 3 %** av kapitalen per handel, maks **12 % bid-ask spread**, kun likvide kortløpende opsjoner, og full logging i **DuckDB Backtracking Vault** (`options_intraday_trades.duckdb`).

### C. De 4 Sikkerhetsportene som Beskytter Kapitalen (Hurdle Rate & Skatt)
Før en intradagsagent får lov til å handle, må signalet bestå fire deterministiske tester:
1. **Hurdle Rate (Lønnsomhetskrav):** Forventet nettogevinst **etter spread, SEC/FINRA/OCC-avgifter og 37,84 % norsk skatt** må være minst **`+0,40 %`** og **`Risk/Reward ≥ 1,8x`**. Små, dyre «støyhandler» avvises automatisk (VETO).
2. **Maks 10 % Cash-Allokering:** Aksjeagenten bruker aldri mer enn 10 % av porteføljen på én enkelt intradagshandel, og sjekker alltid ledig cash hos Alpaca Paper først.
3. **Breakeven-beskyttelse (`+1.0R`):** I `backtest_intraday_duckdb.py` flyttes Stop-Loss automatisk til inngangskurs når handelen har steget med 1× risikoen — da kan ikke en vinner bli til et tap.
4. **Rolig Telegram-rapportering:** De autonome intradagsagentene sender kun melding på Telegram når en posisjon faktisk **lukkes / realiseres** (med realisert P/L før og etter 37,84 % skatt), slik at du slipper støy gjennom dagen.

> **Tips for testing:** Åpne **`04 US Intraday`** i venstremenyen og klikk **▶ Kjør DuckDB ORB Backtest** for å se hvordan 15-min ORB-strategien ville gjort det over de siste handelsdagene — inkludert friksjon og 37,84 % norsk skatt!

---

## 6. Hvordan Evaluere Systemet Hver Søndag

Et tradingsystem må overvåkes — ikke stå på autopilot uten kontroll. Hver søndag sjekker du tre nøkkeltall i **AI Trading Desk**:

1. **Sortino Ratio (Mål: > 1,20):** Måler avkastning kun mot *nedside-svingninger* (ekte tap), ikke mot oppturer. Viser om tapene kuttes raskt mens vinnerne får løpe.
2. **Cost Drag (Mål: < 20 %):** Hvor stor andel av bruttogevinsten som forsvinner i spread/gebyrer.
3. **Shadow Tracking (A/B-test av AI-filteret):** Systemet logger også alle signaler AI-en *avviste*. Hvis de avviste signalene i ettertid falt kraftig, beviser det at AI-filteret sparte deg for penger. Hvis de avviste signalene steg mye, er AI-prompten for streng og bør justeres.

---

## 7. Oppstartssjekkliste for Nybegynnere (5 Steg)

1. **Start alltid i Paper Trading:** Systemet er hardkodet mot **Alpaca Paper** (`paper-api.alpaca.markets`) med fiktive `$100 000` USD — øv deg risikofritt i 4–6 uker.
2. **Verifiser nøklene i `.env`:** Legg inn `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` (Paper), `TELEGRAM_BOT_TOKEN` og `TELEGRAM_CHAT_ID`.
3. **Test en strategiordre:** Gå til **03 Trading** på `http://localhost:8000/`, velg f.eks. *1. Liquid S&P Momentum* med `$50 000`, klikk **📲 Send til Telegram for Godkjenning**, og godkjenn på mobilen.
4. **Sjekk porteføljesynk & OCO-beskyttelse:** Bekreft i **02 Portfolio** at posisjonene dukker opp automatisk med tilhørende Stop-Loss og Take-Profit.
5. **Les regelverket og kjør søndagsrevisjon:** Gjør deg kjent med [trading_agent_rulebook.md](trading_agent_rulebook.md) og [USER_GUIDE.md](USER_GUIDE.md), og vurder resultatene fast hver søndag.
