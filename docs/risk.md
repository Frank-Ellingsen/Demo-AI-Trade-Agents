# Risikostyring: Systematisk (Markedsrisiko) vs. Idiosynkratisk (Selskapsrisiko)

I dette systemet måles og skilles **markedsrisiko (systematisk risiko)** og **selskapsrisiko (idiosynkratisk risiko)** systematisk på fire nivåer: gjennom avkastningsattribuering i `DuckDB`, statistisk multifaktor-regresjon i Python, porteføljesperrer, og kvalitativ hendelsesfiltrering i AI-motoren.

---

## 1. Dekomponering via Attributtanalyse (`DuckDB`)
Gjennom en SQL-basert faktormodell i `DuckDB` brytes hver enkelt handels avkastning og risiko ned i tre distinkte komponenter:

$$\text{Total Nettoavkastning} = \text{Markedsbidrag (OBX)} + \text{Sektorbidrag} + \text{Ren Strategi-Alfa}$$

* **Markedsrisiko / Markedsbeta (OBX-bidrag):** Måler hvor stor del av bevegelsen som utelukkende skyldes at hele Oslo Børs beveget seg i samme holdeperiode. Hvis en handel ga $+1.40 \%$ avkastning, men OBX steg $+1.10 \%$ i samme tidsrom, skyldes nesten hele gevinsten generell markedsmedvind snarere enn en god strategi.
* **Sektor- og råvarerisiko:** Måler meravkastningen sektoren ga utover hovedindeksen (f.eks. energi `OBEP` eller sjømat `OBSE`). På Oslo Børs avslører dette om strategien i realiteten bare rir på oljeprisoppgang eller kronesvekkelse.
* **Selskapsrisiko / Idiosynkratisk Alfa:** Restleddet etter at marked og sektor er trukket fra (fratrukket kurtasje og spread). Det måler om inngangstidspunktet (timing) og selve aksjevalget (`RSI(2) < 10`, `20d Breakout` eller `Bollinger Squeeze`) skapte verdier uavhengig av markedsretningen.

---

## 2. Statistisk Faktorregresjon (Alfa vs. Beta i Python)
For å tallfeste eksponeringen over hele testperioden kjøres en multippel lineær regresjon (Oslo Børs-tilpasset faktormodell) mot porteføljens daglige avkastning:

$$R_{p, t} - R_{f, t} = \alpha + \beta_{\text{OBX}} (R_{\text{OBX}, t} - R_{f, t}) + \beta_{\text{Brent}} \Delta \text{Brent}_t + \beta_{\text{FX}} \Delta \text{USDNOK}_t + \epsilon_t$$

* **$\beta$-koeffisientene (Markeds- og makrorisiko):** Viser hvor sensitiv porteføljen er for systematiske sjokk på Oslo Børs ($\beta_{\text{OBX}}$), svingninger i Brent-olje ($\beta_{\text{Brent}}$) og valutaendringer ($\beta_{\text{FX}}$).
* **Residualene ($\epsilon_t$) og $\alpha$ (Selskapsrisiko og ren merverdi):** Spredningen i residualene ($\sigma_\epsilon$) måler den rene selskapsspesifikke volatiliteten som ikke kan forklares av makrobildet, mens skjæringspunktet $\alpha$ viser den risikokorrigerte meravkastningen systemet genererer utover makroeksponering.

---

## 3. Portefølje- og Risikovern (Korrelasjonskontroll)
På Oslo Børs vil enkeltaksjers tekniske signaler ofte utløses samtidig dersom en underliggende råvare beveger seg kraftig (f.eks. vil `EQNR`, `AKRBP` og `SUBC` ofte gi kjøpssignal samtidig ved en oljekorreksjon eller et oljerally). Uten risikostyring ville en rent endogen modell tatt inn alle disse og skapt en uønsket konsentrasjon i felles råvarerisiko:

* **Sektorbegrensning:** Porteføljemotoren (`allocator.py`) håndhever en hard grense på maks **4 samtidige posisjoner** og maks **2 per sektor**, slik at porteføljen tvinges til risikospredning. Ved flere samtidige signaler brukes deterministisk `priority_rank`.
* **Eksogene makrosperrer:** Strategier med eksogene filtre (`exogenous.py`) krever at `OBX > SMA(200)` og at `VIX < 25` (med unntak for Energi/Shipping ved oljeprisoppgang), samt at 2-dagers Brent-fall ikke overstiger $-2.5 \%$ for energiaksjer. Dette stenger systematisk ned kjøpssignaler under brede makrofall.

---

## 4. AI-Kvalitetskontroll og Shadow Tracking (`EARNINGS_SOON` vs. `MACRO_NOISE`)
Agent 3 (AI-Risikomanager) brukes spesifikt til å vurdere **selskapsspesifikk halerisiko** som tekniske indikatorer er blinde for:

* **Selskapsrisiko:** Modellen sjekker NewsWeb-børsmeldinger (siste 72t) og regnskapskalenderen for selskapshendelser som kvartalsrapporter innen 3 dager (`EARNINGS_SOON`), resultatvarsler, kontraktskanselleringer, emisjonsvarsler eller rettstvister (`COMPANY_SHOCK`).
* **A/B-avslagskontroll i `DuckDB`:** Ved hjelp av ukentlig Shadow Tracking i `signal_evaluations` og `trade_ledger` måles det om AI-avslag skyldes:
  1. `EARNINGS_SOON`: Kvartalstall innen 3 handelsdager.
  2. `COMPANY_SHOCK`: Konkret negativ børsmelding på selskapet.
  3. `MACRO_NOISE`: Generell makrouro (som allerede er priset inn eller filtrert av `OBX`/`VIX`-regimet).
* **Prompt-kalibrering:** Dersom avslag tagget med `MACRO_NOISE` viser seg å blokkere lønnsomme handler i Kohort C (skyggeporteføljen), strammes systemprompten inn til å ignorere generell makrostøy og utelukkende avslå ved harde selskapsspesifikke hendelser (`EARNINGS_SOON` og `COMPANY_SHOCK`).
