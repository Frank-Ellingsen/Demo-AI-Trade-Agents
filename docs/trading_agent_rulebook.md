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

### Agent 4: Execution & Slippage Controller
* **Ordretype:** Kun Limit-ordrer med meglernative OCO brackets (Limit + Stop-Loss + Målkurs).
* **Eksekveringsgrense:** Ingen ordre sendes uten godkjenning via Telegram HITL eller reserve e-post.

---

## 2. Strategiregler

| Parameter | US Liquid Momentum | OBX 15 Momentum | AI Regime Switcher |
| :--- | :--- | :--- | :--- |
| **Univers** | S&P 500 / Nasdaq 100 (> $50M daglig) | Topp 15 OSE-aksjer | SPY, QQQ, T-Bills, Calls |
| **Megler / Valuta** | Alpaca ($0 kurtasje, USD) | Saxo Bank (ASK, NOK) | Alpaca |
| **Spesialfilter** | VIX < 22 og SPY > SMA200 | 2-dagers Brent Crude > -2.5% | VIX-bånd (18 / 24) |
| **Stop-Loss** | $1.8 \times \text{ATR}(14)$ | $2.0 \times \text{ATR}(14)$ | Dynamisk regime |
| **Reward/Risk** | 2.2 : 1 | 2.0 : 1 | Asymmetrisk |

---

## 3. Redundant Godkjenningskjede (Failover)
1. **Primær (Telegram):** Interaktive knapper med 5 minutters timeout.
2. **Sekundær (E-post):** Formatert HTML med sikre `/approve` og `/reject` lenker.
3. **Tertiær (Web Terminal):** Godkjenningsknapp i det visuelle dashbordet.
