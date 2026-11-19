# Brukertestprotokoll: AI Trading Terminal (Nybegynner / Ikke-Proff Bruker)

**Dokumentformål:** Teste brukervennlighet, intuitivitet, informasjonsarkitektur, terminologi og veiledning for en fersking som skal navigere handelsterminalen, velge sin første strategi og gjennomføre sin første simulerte handel (Paper Trading).  
**Målgruppe:** Privat investor / nybegynner uten erfaring med kvantitativ trading, meglernative API-er eller avanserte derivater.  
**Testmiljø:** Web-dashbord (`index.html`) på PC/laptop med tilhørende pop-up strategi-rådgiver og Telegram/e-post godkjenningsvarsling.  
**Testkapital:** $100 000 USD simulert portefølje (Paper Trading).

---

## 1. Testoversikt og Rammebetingelser

### 1.1 Testpersonens Profil
* **Bakgrunn:** Har handlet enkle aksjer/fond via mobilbank eller Nordnet, men har aldri programmert tradingalgoritmer eller brukt profesjonelle verktøy som Bloomberg Terminal eller TradingView.
* **Mål:** Ønsker å forstå hva systemet gjør, føle seg trygg på at roboten ikke gambler bort pengene, forstå risikoen før en handel godkjennes, og kunne styre systemet uten frykt for å trykke feil.

### 1.2 Testlederens Rolle
* Observere passivt («Tenk høyt»-metodikk / *Think Aloud Protocol*).
* Ikke hjelpe brukeren med navigasjon med mindre brukeren står fullstendig fast i mer enn to minutter.
* Notere hvor brukeren nøler, misforstår faguttrykk, leter etter knapper, eller opplever feil (bugs).

---

## 2. Testfase 1: Førsteinntrykk, Navigasjon og Menystruktur

**Oppgave:** Åpne dashbordet, se deg rundt i 60 sekunder, og fortell hva du ser uten å klikke på noe ennå. Deretter skal du navigere gjennom menyene i venstre kolonne.

### Sjekkpunkter for observasjon:
| Område | Hva testes? | Forventet oppførsel | Brukerens opplevelse (Notater) | Bestått (Ja/Nei) |
| :--- | :--- | :--- | :--- | :--- |
| **Visuell støy** | Virker skjermen overveldende eller ryddig? | Brukeren finner raskt porteføljeverdien ($100 000) og dagens resultat. | | |
| **Venstremeny** | Er ikonene og menytekstene selvforklarende? | Brukeren forstår rekkefølgen: *01 Oversikt*, *02 Portefølje*, *03 Handel & Ordrer*, *04 Markeder*, *05 Analyse & Strategier*. | | |
| **Konsistens** | Stemmer overskriften med innholdet i panelet? | Når brukeren klikker på *04 Markeder*, forventer de markedskurser og watchlister, ikke teknisk systemfeilsøking. | | |
| **Aktiv fane** | Skjønner brukeren hvor i systemet de befinner seg? | Venstremenyen markerer tydelig aktiv side med farge/bakgrunn. | | |

---

## 3. Testfase 2: Begrepsforståelse, Tooltips og Veiledning (Guidance)

**Oppgave:** Finn frem til nøkkeltall og innstillinger. Les forklaringene som dukker opp når du holder musepekeren over ikoner eller begreper (Tooltips).

### Faguttrykk som skal testes mot nybegynneren:
1. **Stop-Loss & Take-Profit:**
   * *Spørsmål til bruker:* «Hva skjer hvis aksjen faller etter at du har kjøpt?»
   * *Mål:* Brukeren skal forstå at systemet kutter tapet automatisk, og ikke tro at de kan tape ubegrenset.
2. **ATR (Average True Range):**
   * *Spørsmål til bruker:* «Hva tror du 'ATR-stopp' betyr ut fra hjelpeteksten?»
   * *Mål:* Brukeren skal skjønne at sikkerhetsmarginen tilpasses aksjens normale svingninger, uten å måtte regne på formelen selv.
3. **Markedsregime & VIX:**
   * *Spørsmål til bruker:* «Hva betyr det når det står 'Bull Regime (VIX 16.4)' med grønt lys?»
   * *Mål:* Skjønner brukeren at dette betyr stabilt vær i aksjemarkedet?
4. **Paper Trading vs. Ekte Penger:**
   * *Spørsmål til bruker:* «Er pengene du ser på skjermen ekte sparepenger eller treningspenger?»
   * *Mål:* Ingen tvil om at dette er en risikofri simuleringskonto.

---

## 4. Testfase 3: Velge og Forstå en Strategi (Bruk av Pop-up Rådgiver)

**Oppgave:** Gå til *05 Analyse & Strategier*. Vurder de ulike strategiene (US Liquid Momentum, Balansert profil, Dekkede Opsjoner). Bruk deretter den flytende chat-rådgiveren nede i høyre hjørne for å få råd om hva du bør velge.

### Testoppgaver for brukeren:
* **Steg 1: Velg risikoprofil:** Bytt mellom *Konservativ*, *Balansert* og *Aggressiv*.
  * *Observasjon:* Forstår brukeren umiddelbart hva som endrer seg (f.eks. mer kontantbuffer ved konservativ profil)?
* **Steg 2: Åpne AI Rådgiver (Pop-up Chat):** Klikk på chat-ikonet nede i høyre hjørne.
* **Steg 3: Still et spørsmål:** Trykk på hurtigknappen *«Analyser US Momentum»* eller skriv: *«Hva er nedsiden med Momentum-strategien?»*
* **Steg 4: Vurder svaret:**
  * Føles rådet betryggende og forståelig?
  * Forklarer rådgiveren risikoen på vanlig norsk uten uforståelig finanssjargong?

---

## 5. Testfase 4: Gjennomføre Første Handel & Godkjenning (HITL)

**Oppgave:** Legg inn en simulert kjøpsordre på **NVDA (Nvidia)** eller **EQNR (Equinor)** basert på anbefalt oppsett.

### Testflyt:
1. **Ordreoppsett i panelet (*03 Handel & Ordrer*):**
   * Fyll inn ticker: `NVDA`.
   * Velg antall aksjer (f.eks. `10 stk`).
   * Observer at Limit-kurs, Stop-Loss og Take-Profit foreslås automatisk.
   * *Brukertilbakemelding:* Er det klart hvor mye handelen koster totalt i dollar/kroner før du trykker send?
2. **Utsending til godkjenning:**
   * Trykk på knappen **«Klargjør Ordre (Send til HITL)»**.
3. **Varsling og Vurdering:**
   * Sjekk varselet som mottas (i Telegram på mobil eller i test-modalen i dashbordet).
   * Viser varselet tydelig: Hva kjøpes? Hva er maks tap i kroner? Hvorfor anbefales handelen?
4. **Beslutning:**
   * Trykk på den grønne knappen `[✅ Godkjenn]`.
   * Bekreft at ordren dukker opp som aktiv posisjon under *02 Portefølje*.

---

## 6. Testfase 5: Feilsituasjoner, Uforutsette Hendelser og Bugsjekk

**Oppgave:** Prøv bevisst å gjøre vanlige feil for å se om systemet veileder deg vennlig eller krasjer.

| Handling | Forventet systemrespons | Faktisk observasjon / Bug funnet |
| :--- | :--- | :--- |
| Skrive inn en ugyldig ticker (f.eks. `XYZ123`) | Rødt varsel: «Ugyldig aksjesymbol, vennligst sjekk tickeren.» | |
| Prøve å kjøpe for mer enn tilgjengelig saldo (f.eks. $200 000) | Blokkerende melding: «Utilstrekkelig kjøpekraft. Maksimal ordreverdi er $X.» | |
| Sette Stop-Loss høyere enn kjøpskursen | Input-validering: «Stop-Loss må ligge under kjøpskurs for et kjøp.» | |
| Klikke raskt flere ganger på kjøpsknappen | Knappen deaktiveres midlertidig for å hindre duplikathandler. | |
| Trykke `[❌ Avvis]` i godkjenningsvarselet | Ordren kanselleres uten feilmelding; ingen penger trekkes. | |

---

## 7. Evalueringsskjema (Brukerens Egen Vurdering etter Testen)

Brukeren fyller ut denne delen umiddelbart etter at testen er fullført (Skala 1 til 5, der 1 = Svært dårlig / Uenig, 5 = Svært bra / Helt enig).

| Kriterium | Score (1–5) | Begrunnelse / Kommentar |
| :--- | :---: | :--- |
| **1. Oversikt og ryddighet:** Skjermen var behagelig å se på, uten for mye støy. | | |
| **2. Enkel navigasjon:** Jeg skjønte alltid hvor jeg var og hvordan jeg kom tilbake. | | |
| **3. Forståelige ord:** Tekster og forklaringer var skrevet på et språk jeg forsto. | | |
| **4. Følelse av trygghet:** Jeg følte full kontroll over at penger ikke forsvant av seg selv. | | |
| **5. AI-rådgiveren:** Chat-boten svarte relevant og hjalp meg å forstå valgene mine. | | |
| **6. Godkjenningsprosessen:** Det var enkelt å godkjenne eller avvise handelen på mobilen/skjermen. | | |

---

## 8. Brukerens Anbefalte Forbedringer (Sluttrapport)

*Dette fylles ut av testlederen sammen med brukeren i en 5-minutters oppsummeringssamtale etter testen.*

### 1. Hva var det beste eller mest intuitive med appen?
*(F.eks. «Godkjenningskortet på Telegram med tydelig maks tap i kroner gjorde meg trygg.»)*
> Svar: 

### 2. Hva var mest forvirrende eller vanskelig å forstå?
*(F.eks. «Forkortelsene ATR og Drawdown burde hatt en enklere forklaring rett i tabellen.»)*
> Svar: 

### 3. Opplevde du noen tekniske feil, hakkete animasjoner eller knapper som ikke virket?
> Svar: 

### 4. Konkrete endringsforslag før lansering:
1. **Endring 1 (Tekst / Språk):** 
2. **Endring 2 (Design / Knappeplassering):** 
3. **Endring 3 (Veiledning / Nybegynner-hjelp):** 
