# Eksplisitt Rulebook for AI-Tradingagenter & Strategier

Dette dokumentet definerer det operative regelverket for handel, risikostyring og agentmandater i systemet.

---

## 1. Multi-Agent Meglerbord (Roller & Veto-regler)

### Agent 1: Sentiment & News Analyst
* **Kilder:** Massive/Polygon News, SEC/NewsWeb meldinger.
* **Hard Veto:** Dersom et selskap legger frem kvartalsrapport innen de neste 48 timene, blokkeres handelen umiddelbart (*Earnings Trap Protection*).

### Agent 2: Quantitative Signal Engine
* **Metodikk:** 12-1 måneders momentum (hopper over ferskeste måned for å unngå reversering), kombinert med RSI(2) og Frog-in-the-Pan (FIP) jevnhetsmål.
* **Krav:** Aksjen må handle over sitt 200-dagers glidende gjennomsnitt ($P > \text{SMA200}$) og volum > 1.2x SMA20.

### Agent 3: Risk & Regime Manager
* **ATR-posisjonering:** Stop-loss settes alltid dynamisk til $1.8 \times \text{ATR}(14)$ for balansert profil.
* **Sektortak:** Maksimalt 2 posisjoner eller 35 % samlet kapital i én sektor.
* **Circuit Breaker:** Porteføljetap over 6 % kutter automatisk alle nye innganger.

### Agent 4: Execution, Cost Hurdle & Slippage Controller
* **Deterministisk Cost & Tax Hurdle (`src/portfolio/tax_fee_fx_engine.py`):** Ingen ordre sendes til HITL før $\mathbb{E}[\text{Alpha}_{\text{gross}}] > C_{\text{all-in}} + \text{Alpha}_{\text{buffer}}$.
* **Ordretype:** Kun Limit-ordrer med live Alpaca Market Data (`+0,1 %` marketable limit) og meglernative OCO brackets (Limit + Stop-Loss + Take-Profit).
* **Eksekveringsgrense:** Ingen ordre sendes uten eksplisitt godkjenning via Telegram HITL, backup e-post eller Web Terminal.

---

## 2. Strategiregler (US, Norden OMXN40 & Norge OBX 15)

| Parameter | US Liquid Momentum | Norden (OMXN40) | Norge (OBX 15) | AI Regime Switcher |
| :--- | :--- | :--- | :--- | :--- |
| **Univers** | S&P 500 / Nasdaq 100 (> $50M daglig) | OMX Nordic 40 (40 selskaper: DK, SE, FI) | Topp 15 OSE-aksjer | SPY, QQQ, T-Bills, Calls |
| **Anbefalt Konto / Megler** | **Alpaca Paper** (Dedikert USD-pool, $0 kurtasje) | **Nordnet ASK** (EØS, 0 % løpende skatt) | **Nordnet ASK** (NOK, 0 % FX) | **Alpaca Paper** |
| **Spesialfilter** | VIX < 22 og SPY > SMA200 | Nordic FX-drag & trend over SMA200 | 2-dagers Brent Crude > -2.5% | VIX-bånd (18 / 24) |
| **Stop-Loss** | $1.8 \times \text{ATR}(14)$ | $1.5\text{–}1.8 \times \text{ATR}(14)$ | $2.0 \times \text{ATR}(14)$ | Dynamisk regime |
| **Reward/Risk** | 2.2 : 1 | 2.0 : 1 | 2.0 : 1 | Asymmetrisk |

---

## 3. Redundant Godkjenningskjede (3-Trinns Failover & Bekreftelse)
1. **Primær (Telegram HITL):** Interaktive knapper (`✅ Godkjenn` / `❌ Avvis`) med umiddelbar bekreftelsesmelding og tidsstempel (`YYYY-MM-DD HH:MM:SS`), pluss automatisk varsel ved åpnet/lukket posisjon (`🔔 POSISJONSENDRING`) og daglig rapport kl. **22:15 CET**.
2. **Sekundær (Backup E-post / SMTP):** Formatert HTML-epost via `failover_approver.py` med signerte `/approve`- og `/reject`-lenker og tidsstempel (`decided_at`).
3. **Tertiær (Web Terminal):** Direkte godkjennings- og avvisningskø i dashbordet (`http://localhost:8000/`).
