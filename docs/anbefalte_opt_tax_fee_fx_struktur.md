# Optimal Skatte-, Gebyr- og Valutastruktur (Tax, Fee & FX Architecture)

Dette dokumentet formaliserer den operasjonelle og regnskapsmessige rutingmodellen for kvantitative tradingagenter og porteføljestyring på tvers av norske, nordiske og amerikanske markeder. Dokumentet etablerer eksplisitte regler for kontotyper, meglervalg, likviditetsjusterte kostnadsfunksjoner og avklarer hvorfor denne arkitekturen implementeres som en **deterministisk regelmotor** fremfor en autonom LLM-agent.

---

## 1. Strategisk Allokeringstabell per Markedssegment

For å maksimere netto risikojustert avkastning (*Net Alpha*) etter all friksjon må hver aktivaklasse rutes til riktig juridisk kontotype og utførelsesmegler:

| Hva du handler | Børs / Marked | Konto / Provider | Primær Begrunnelse | Valuta & Skattefordel |
| :--- | :--- | :--- | :--- | :--- |
| **Norske Large / Mid-cap** | Oslo Børs (OSE) | **Nordnet ASK** | Hjemmemarked, direkte oppgjør i NOK uten valutaveksling. | Utsatt beskatning via ASK; ingen løpende gevinstskatt ved rebalansering. |
| **Svenske aksjer** | Nasdaq Stockholm | **Nordnet ASK** | Primærnotering i SEK, tilgang til bredt nordisk likviditetsbasseng. | EØS-kvalifisert for ASK; 0 % løpende gevinstbeskatning før uttak. |
| **Danske aksjer** | Nasdaq Copenhagen | **Nordnet ASK** / Saxo ASK | Primærnotering i DKK. | EØS-kvalifisert for ASK; Saxo har lavere minstekurtasje på enkeltordrer under 20 000 DKK. |
| **Finske aksjer** | Nasdaq Helsinki | **Nordnet ASK** | Primærnotering i EUR. | EØS-kvalifisert for ASK; automatisk integrert skatterapportering. |
| **EØS-aksjer ellers** | Primæreuropeiske børser | **Nordnet ASK** (hvis kvalifisert) | Kun selskaper med skattemessig hjemmehørighet i EØS. | Skatteutsettelse dominerer transaksjonsfriksjon over tid. |
| **US Mega-cap / AI-aksjer** | NYSE / Nasdaq | **Alpaca Markets** | Svært lave handelsgebyrer ($0 provisjon på standardaksjer) + API bygget for automatisering. | Beholdes i en dedikert USD-kontantbuffer for å unngå tur-retur FX ved hver handel. |
| **Aktiv Algoritmisk US-trading** | NYSE / Nasdaq | **Alpaca Markets** | Høyfrekvente API-grenser (opptil 10 000 RPM) og meglernative OCO bracket-ordrer. | Ideelt for momentum og intradag-eksekvering der kurtasje ellers ville spist alfaen. |
| **EØS-selskap kryssnotert i USA** | F.eks. Equinor (EQNR) på OSE vs. NYSE | **Nordnet ASK (OSE)** | Skattefordelen ved ASK overgår marginalluksusen med $0 provisjon i USA. | Unngår 0,25–0,50 % FX-spread og amerikansk kildeskatt; handler i primærnotering. |
| **Globale markeder utenfor USA/Norden** | Lokale børser (UK, Asia, Sveits) | **Saxo Bank** | Tilgang til 50+ globale børser, opsjonskjeder og obligasjoner. | Fleksibel multi-valutakonto, men belastes med 0,12 % årlig depotgebyr på norske depoter. |
| **Amerikanske ADR-er** | NYSE / Nasdaq | **Kun ved særskilt likviditetsfordel** | ADR-er tilfører et ekstra depot-/gebyrlag og kvalifiserer ikke for ASK dersom selskapet er utenfor EØS. | Unngås som standard med mindre primærmarkedet er utilgjengelig eller illikvidt. |

---

## 2. All-in Kostnadsmodell (Transaction Cost & Tax Engine)

En kjernefeil i mange uerfarne kvantsystemer er å optimalisere mot meglere med «$0 provisjon» uten å ta hensyn til bid-ask spread, markedsimpakt, vekslingspåslag og skattetap. Den totale friksjonen for en full tur-retur-handel (*round-trip*) modelleres matematisk som:

$$C_{\text{all-in}} = C_{\text{commission}} + C_{\text{spread}} + C_{\text{slippage}} + C_{\text{FX}} + C_{\text{regulatory}} + C_{\text{custody}} + C_{\text{tax\_drag}}$$

### Komponentenes definisjon og oppførsel:

1. **Meglerkurtasje ($C_{\text{commission}}$):**
   $$C_{\text{commission}} = \max(V_{\text{trade}} \times r_{\text{rate}}, \text{Floor}_{\text{min}})$$
   * Nordnet Normal: 0,049 % (min. 79 NOK) | Nordnet Mini: 0,15 % (min. 29 NOK).
   * Saxo Classic: 0,08 % (min. 10 NOK / $1 USD) | Saxo VIP: 0,03 %.
   * Alpaca: $0 baseprovisjon på aksjer for personlige privatkunder.

2. **Bid-Ask Spread ($C_{\text{spread}}$):**
   Den reelle prisen for umiddelbar likviditet:
   $$C_{\text{spread}} = V_{\text{trade}} \times \left(\frac{P_{\text{ask}} - P_{\text{bid}}}{P_{\text{mid}}}\right)$$
   På illikvide aksjer (f.eks. vekstselskaper eller OTC) kan denne utgjøre 1,5–3,0 %, noe som gjør kurtasjebesparelser fullstendig irrelevante.

3. **Gjennomføringsslippage ($C_{\text{slippage}}$):**
   Avviket mellom signalets beslutningskurs og faktisk fylt kurs i ordreboken:
   $$\text{Slippage} = \frac{|P_{\text{fill}} - P_{\text{decision}}|}{P_{\text{decision}}}$$
   Minimeres ved å benytte aggresjonsbegrensede Limit-ordrer med dynamisk offset basert på $\text{ATR}(14)$.

4. **Valutafriksjon ($C_{\text{FX}}$):**
   * Automatisk veksling hos Nordnet og Saxo: **0,25 % per side** (0,50 % tur-retur).
   * Manuell valutakonto (underdepot): Reduserer påslaget til **0,15 % per side**.
   * **Strukturelt prinsipp for USA-trading:** Alloker en fast kapitalbase i USD hos Alpaca som får forbli i USD over tid, fremfor å veksle NOK $\leftrightarrow$ USD ved hver transaksjon.

5. **Regulatoriske avgifter ($C_{\text{regulatory}}$):**
   I USA påløper SEC Transaction Fee (ved salg), FINRA Trading Activity Fee (TAF) og Consolidated Audit Trail (CAT)-avgifter. Disse videreføres direkte til kunden.

6. **Løpende depotgebyr ($C_{\text{custody}}$):**
   * Nordnet: 0 kr for nordiske investorer.
   * Saxo Bank: 0,12 % p.a. på aksjer og ETF-er for norske kunder (med mindre aksjeutlån aktiveres der det tilbys). På en portefølje på 1 000 000 NOK utgjør dette 1 200 NOK årlig, uavhengig av handelsaktivitet.

7. **Skatteforsinkelse og timingfordel ($C_{\text{tax\_drag}}$):**
   * **Inne på ASK:** $\text{Umiddelbar skatt} = 0$. Gevinster reinvesteres ubeskåret med full renters-rente-effekt inntil kapital tas ut av kontoen.
   * **På ordinær VPS/verdipapirkonto:** Hver realiserte gevinst beskattes i det aktuelle inntektsåret (effektiv skattesats for aksjeinntekter er 37,84 % etter oppjusteringsfaktor i 2026), noe som reduserer den frie vekstkapitalen ved hyppig rebalansering.

---

## 3. Filtreringsregel for Handelsbeslutninger (Trade Hurdle Rule)

Ingen ordre skal sendes til markedet kun fordi et momentum- eller reverseringssignal er utløst. Agentens kostnadsmotor krever at forventet bruttoalfa overskrider totalkostnaden pluss en sikkerhetsbuffer:

$$\text{Trade Condition} \iff \mathbb{E}[\text{Alpha}_{\text{gross}}] > C_{\text{all-in}} + \text{Alpha}_{\text{buffer}}$$

*Eksempel:* Hvis beregnet $C_{\text{all-in}} = 0,35\ \%$ og minimumsbufferen er satt til $0,20\ \%$, må strategiens predikerte gevinst overstige **0,55 %** for at ordren skal godkjennes og sendes til Telegram HITL.

---

## 4. Hvorfor Tax, Fee & FX er en Deterministisk Regelmotor (og IKKE en Egen LLM-Agent)

Spørsmålet om hvorvidt skatte-, gebyr- og valutahåndtering krever en autonom AI/LLM-agent besvares entydig med **nei**. Dette domenet skal styres av en **deterministisk regel- og kalkulasjonsmotor**, plassert som en hard portvakt (*Transaction Cost & Routing Engine*) mellom signalgenereringen og ordreutførelsen.

### Begrunnelse for arkitekturvalget:

```
[Signal / Strategi] ──(Kandidat: Ticker, Målvolum, Retning)──┐
                                                             │
                                                             ▼
                                     ┌───────────────────────────────────────┐
                                     │  DETERMINISTISK COST & ROUTING ENGINE │
                                     │  1. Sjekk EØS/ASK-kvalifisering       │
                                     │  2. Beregn Minstekurtasje vs. Provisjon│
                                     │  3. Kalkuler Spread & Volumandel      │
                                     │  4. Evaluer FX-drag vs. USD-pool      │
                                     │  5. Net Alpha > All-In Cost?          │
                                     └──────────────────┬────────────────────┘
                                                        │
                              ┌─────────────────────────┴─────────────────────────┐
                              ▼ (Passerer terskel)                                ▼ (Feiler terskel)
                     [Telegram HITL Kort]                                   [Ordre Avvist lokalt]
```

1. **Deterministisk matematikk vs. stokastisk gjetning:**  
   Beregning av minstekurtasje (`max(volum * 0.00049, 79)`), vekslingsgebyr (0,25 %) og bid-ask spread er eksakte formler. Å la en sannsynlighetsbasert språkmodell (LLM) gjette eller kalkulere gebyrer innfører risiko for hallusinasjoner og feilavrundinger.

2. **Binære regulatoriske regler:**  
   Hvorvidt et selskap er ASK-kvalifisert er en binær egenskap (er selskapet skattemessig registrert og hjemmehørende innenfor EØS eller ikke?). Dette løses optimalt via en oppslagstabell eller felt i DuckDB/databasen, ikke ved skjønnsmessig teksttolkning.

3. **Latens og datakostnader:**  
   En deterministisk Python-funksjon evaluerer kostnad og ruting for 100 aksjer på under **1 millisekund** uten nettverkskall eller tokenkostnader. En LLM-agent ville brukt 500–2 000 ms per vurdering og økt driftskostnadene betydelig.

4. **Revisjonssikkerhet og samsvar (Audit Trail):**  
   Skattelovgivning og meglervilkår krever full sporbarhet. Skulle en handel revideres, må det kunne bevises nøyaktig hvilke satser som ble brukt i rutingkalkylen.

### Hvor LLM-agentene faktisk hører hjemme:
* **Sentiment Analyst:** Tolke nyhetsartikler, børsmeldinger og identifisere selskapsspesifikke feller (*Earnings Trap*).
* **Devil's Advocate:** Kvalitativ utfordring av teser som ser kvantitativt sterke ut, men som har strukturelle eller uventede regulatoriske svakheter.
* **Strategi- & Risikorådgiver (Pop-up Chat):** Forklare mekanismene for brukeren i naturlig språk, simulere porteføljeresultater og gi interaktiv veiledning i dashbordet og på Telegram.
