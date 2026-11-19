# UI/UX Brukertest og Beslutningsprotokoll: Telegram Human-in-the-Loop Trading

## 1. Vurdering og Kognitiv Gjennomgang av Varslingsformatet

I en automatisert handelsrigg med Human-in-the-Loop fungerer mobilgrensesnittet som systemets viktigste sikkerhetsmekanisme. Når en algoritme identifiserer en handelsmulighet, skal ikke brukeren utføre en full teknisk analyse på mobilskjermen. Brukerens rolle er å opptre som en overordnet risikomanager. Beslutningstakeren må i løpet av få sekunder kunne verifisere tre avgjørende elementer:

1. Hvor mye kapital bindes opp, og hva er det maksimale tapet ved stop-loss?
2. Hvilken strategi og markedsbetingelse utløste signalet (for eksempel trendutbrudd versus oversolgt reversering)?
3. Hva er motargumentet («Djevelens advokat»), og finnes det umiddelbar hendelsesrisiko som forestående kvartalstall eller unormal markedsuro?

### Identifiserte Svakheter i Enkle Signalvarsler
Enkle signalvarslere viser typisk kun ticker, volum, inngangskurs og målnivåer. Dette skaper usikkerhet for en operatør som skal godkjenne en reell handel:
* **Manglende porteføljesammenheng:** Meldingen opplyser ikke hvor stor prosentandel av totalkapitalen handelen utgjør, eller hvor stor kontantreserve som gjenstår.
* **Fravær av volatilitetsreferanse:** Stop-loss oppgis som et fast kursnivå uten referanse til Average True Range (ATR). Operatøren kan ikke vurdere om stoppet er plassert utenfor normal intradagstøy.
* **Mangel på selskaps- og hendelseskontekst:** Uten angivelse av dager til neste kvartalstall risikerer operatøren å godkjenne posisjoner rett foran uforutsigbare resultatvarsler.
* **Manglende tids- og kursvern:** Dersom markedet stiger kraftig før operatøren rekker å svare, kan en manuell godkjenning føre til uønsket kursslippage.

---

## 2. Beslutningskortets Informasjonsstruktur

For å sikre rask og presis vurdering på mobile enheter deles varselet inn i seks faste informasjonsfelter:

| Felt | Innhold | Formål |
| :--- | :--- | :--- |
| **1. Header & Mandat** | Ticker, markedsplass, strategitype og signal-ID | Etablerer umiddelbar gjenkjenning av hvilket handelsmandat signalet tilhører. |
| **2. Posisjon & Allokering** | Aksjeantall, Limit-kurs, total eksponering og porteføljevekt i % | Bekrefter at posisjonsstørrelsen følger porteføljens allokeringsregler. |
| **3. Risikoparametere (OCO)** | Stop-Loss-kurs, maksimalt tap i dollar/kroner, ATR-avstand, Take-Profit og R/R-forhold | Viser det asymmetriske gevinst/tap-forholdet før ordren sendes. |
| **4. Markedsregime & Trigger** | Benchmark-trend (over/under SMA200), VIX-nivå, relativt volum og dager til kvartalstall | Luker ut handler som går mot den overordnede markedstrenden eller foran binære hendelser. |
| **5. AI-Dobbeltvurdering** | Modellens Bull-case versus Djevelens advokat (hovedrisiko) | Gir et balansert motperspektiv som motvirker bekreftelsesskjevhet. |
| **6. Tidsbegrensning & Slippage** | Utløpsfrist for varselet og maksimalt tillatt kursavvik | Forhindrer at ordren sendes dersom markedskursen har løpt fra limit-nivået. |

---

## 3. Eksempel på Utvidet Telegram-Beslutningskort

Nedenfor vises det utvidede formatet slik det fremstår direkte i Telegram-appen på mobil:

```text
🟢 S&P 500 SIGNAL #1042: NVDA (NVIDIA CORP)
Strategi: Trend Breakout (20d High) | Risiko: MODERAT

📊 POSISJON & ALLOKERING:
• Ordre: Buy Limit @ $145.00 USD
• Volum: 105 aksjer (Totalt: $15,225.00 USD)
• Porteføljevekt: 15.2 % av $100,000 equity (Kontantreserve: 14.8 %)

🛡️ BRACKET RISIKOSTYRING (OCO):
• Stop-Loss: $132.50 (-8.62 % | Maks tap: -$1,312.50 USD)
  ↳ Avstand: 2.1x ATR(14) [Plassert utenfor normal dagsstøy]
• Take-Profit: $172.50 (+18.96 % | Målgevinst: +$2,887.50 USD)
• Reward / Risk: 2.20 : 1.00

🌐 REGIME & MARKEDSKONTEKST:
• S&P 500 Regime: Bull (SPY > SMA200, +0.45 % i dag)
• VIX Volatilitet: 16.20 (Rolig marked, lav opsjonsrisiko)
• Volumkatalysator: 1.48x relativt 20-dagers snittvolum
• Hendelsesrisiko: Neste kvartalsrapport om 28 dager (Ingen umiddelbar risiko)

🧠 AI-ANALYSE (GEMINI FLASH):
📈 Bull-case: Kraftig teknisk utbrudd på høyt volum etter tre ukers konsolidering over 50-dagers snitt.
😈 Djevelens advokat: Halvledersektoren viser kortsiktig divergens; motstand kan oppstå rundt $150-nivået.

⏳ Gyldighet: Utløper automatisk om 15 minutter
⚠️ Kursvern: Ordren kanselleres automatisk dersom Ask overstiger $146.45 (+1.0 %)

[ ✅ GODKJENN BRACKET-ORDRE ]   [ ❌ AVVIS SIGNAL ]
```

---

## 4. Brukertestprotokoll for Testkjøring i `G:\My Drive\AI trader`

For å verifisere at beslutningsprosessen fungerer optimalt, gjennomføres fire simulerte testscenarioer:

### Testscenario 1: Standard Trendutbrudd under Rolige Markedsforhold
* **Mål:** Verifisere rask godkjenning når alle parametere er grønne.
* **Testdata:** Ticker `MSFT`, pris \$425.00, posisjon \$18 000 (18 % vekt), Stop-Loss \$412.00 (1.8x ATR), Take-Profit \$455.00 (R/R 2.3:1). S&P 500 over SMA200, VIX 15.2, dager til earnings: 42.
* **Forventet handling:** Bruker trykker `[✅ GODKJENN]` innen 5 sekunder. Ordren bekreftes med Alpaca Ordre-ID.

### Testscenario 2: Kvartalstall-Fellen («Earnings Trap»)
* **Mål:** Verifisere at hendelsesrisiko oppdages og avvises umiddelbart.
* **Testdata:** Ticker `PLTR`, kurs \$65.00, teknisk utbruddssignal. Varselet opplyser: *«⚠️ Kvartalsrapport legges frem i kveld etter børsslutt»*. Djevelens advokat flagger høy risiko for overnattingsgap.
* **Forventet handling:** Bruker trykker `[❌ AVVIS]`. Systemet logger avvisningen og hindrer kapitaltap fra uforutsigbare resultatgap.

### Testscenario 3: Slippage-Sperre (Kursen har løpt)
* **Mål:** Bekrefte at systemet forhindrer feilaktig fylling ved forsinket respons.
* **Testdata:** Signal generert med Limit \$120.00. Bruker svarer etter 4 minutter, men markedet har hoppet til Ask \$122.50 (+2.1 %).
* **Forventet handling:** Bruker trykker `[✅ GODKJENN]`. Skriptet oppdager at Ask overstiger maks akseptert kursgrense på \$121.20, avbryter ordren lokalt og varsler brukeren i Telegram om at ordren ble kansellert på grunn av slippage.

### Testscenario 4: Sektortak og Korrelasjonsvern
* **Mål:** Hindre at porteføljen overeksponeres mot én enkelt sektor.
* **Testdata:** Porteføljen har allerede to åpne posisjoner i energisektoren (`XOM` og `CVX`). Et nytt utbruddssignal dukker opp i `COP`. Varselet fremhever: *«⚠️ Sektorvarsel: Energisektoren har allerede 2 av 2 tillatte posisjoner»*.
* **Forventet handling:** Bruker trykker `[❌ AVVIS]`, og porteføljens risikobudsjett opprettholdes.

---

## 5. Oppsummering og Anbefalte Rutiner for Windows 11

1. **Filplassering:** Lagre filene i `G:\My Drive\AI trader\` slik at de automatisk sikkerhetskopieres og synkroniseres.
2. **Tidsstyring:** La Windows Oppgaveplanlegger (Task Scheduler) starte Telegram-lytteren kl. 15:15 CET på hverdager (15 minutter før amerikansk børsåpning) og stoppe den ved børsslutt kl. 22:05 CET.
3. **Loggføring:** Alle godkjenninger og avvisninger lagres i DuckDB eller SQLite, slik at du ukentlig kan evaluere effekten av manuelle beslutninger opp mot en helautomatisk strategi.
