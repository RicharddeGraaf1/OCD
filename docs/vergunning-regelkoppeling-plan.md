# Vergunning ↔ regel koppelen via de werkzaamheid — uitvoeringsplan

**Status**: voorstel, nog niet gebouwd · **Datum**: 2026-09-21
**Onderbouwing**: metingen van 2026-09-20 en 2026-09-21 op de lokale OCD-DB
**Meetscript**: `dso-loader/scripts/meet_vergunning_werkzaamheid.py` (~20 s op de
standaard steekproef van 1.000)
**Raakt**: `vth.vergunningkennisgeving` (één nieuwe kolom), nieuwe tabel
`vth.vergunning_werkzaamheid`, nieuwe view `vth.v_vergunning_regeltekst`,
`ocd-api/vergunningen.py`, `omgevingsvergunning-register.nl`

---

## 1. Wat dit plan wel en niet is

Een omgevingsvergunning in `vth.vergunningkennisgeving` staat vandaag volledig
los van de regels waaronder ze is verleend. Dit plan legt die relatie, langs de
**reguliere annotatieketen**:

```
kennisgeving (B1-taal)  ->  Werkzaamheid  ->  Activiteit  ->  ActiviteitLocatie-
aanduiding  ->  JuridischeRegel  ->  Regeltekst  ->  Artikel of Lid
```

De werkzaamheid is daarin geen willekeurig tussenstation maar de enige knoop in
het hele stelsel die in **dezelfde taal** staat als een kennisgeving. *"het
plaatsen van een dakkapel"* tegen *"Dakkapel plaatsen"* is een gelijk-op-gelijk
vergelijking; tegen de juridische activiteitnaam *"bouwen van een bijbehorend
bouwwerk"* is dat niet zo. Daar komt bij dat de moeilijke helft van het werk al
gedaan is: `i2a.werkzaamheid_activiteit` bevat 91.207 met de hand gecureerde
koppelingen, en **per overheid gesneden levert een werkzaamheid mediaan één
activiteit**.

Wat dit plan **niet** doet:

- **Geen ruimtelijke route.** Getoetst en verworpen: de locatiefilter versmalt
  de regellijst niet (mediaan blijft 2, p90 30 → 28), omdat de
  activiteit-annotatie meestal aan het hele ambtsgebied hangt.
- **Geen directe tekst → activiteit-match** (roadmap-item B3 uit de
  vault-analyse *Vergunningkennisgeving — verrijking data en voorziening*). Dat
  is kiezen uit 28.263 activiteiten in juridische taal in plaats van uit 294
  werkzaamheden in B1-taal, en het gooit de gecureerde koppeling weg.
- **Geen antwoord op "waarvan wijkt deze BOPA af".** Dat is een geometrische
  vraag op een andere as; zie §7.
- **Geen embeddings in v1.** Zie fase 6.

## 2. De cijfers waar het plan op staat

Steekproef van 1.000 verleende vergunningen (van 230.747), tekstbron = titel +
beschrijving + de eerste 4.000 tekens `inhoud_tekst`. 95%-marge ±3 procentpunt.

**De trechter.** Alleen de eerste sprong lekt; daarna is het verlies nul:

| stap | dekking |
|---|---:|
| bevoegd gezag geresolved | 97,6% |
| ≥1 werkzaamheid-kandidaat | 84,5% |
| eenduidige werkzaamheid | 57,8% |
| bereikt een activiteit | 57,8% |
| bereikt een juridische regel | 57,8% |
| **bereikt regeltekst (artikel of lid)** | **57,8%** |

**De top-N-knop.** Dit is het getal waar het ontwerp op draait:

| N | dekking | med. activiteiten | med. artikelen | p90 artikelen |
|---:|---:|---:|---:|---:|
| 1 | 59,1% | 1 | 2 | 9 |
| **2** | **80,2%** | 2 | 4 | 10 |
| 3 | 81,3% | 3 | 5 | 11 |
| 5 | 81,4% | 3 | 6 | 11 |
| 10 | 81,4% | 3 | 6 | 11 |

De curve is vlak na N=2. Van 1 naar 2 is **+21,1 procentpunt**; van 2 naar 5 is
**+1,2**. Een top-5 kost drie schermregels voor niets. **Top-2 is de knop.**

**De bodytekst verdubbelt de opbrengst.** Op alleen titel + beschrijving haalt
de eenduidige match 27,9%; met `inhoud_tekst` erbij 57,8%. Die kolom staat sinds
2026-05-30 voor 100% van het register gevuld — geen nieuwe bron, geen model.

**Twee dingen die de vorm bepalen.** De annotatie hangt in 76% aan een **Lid**,
niet aan een Artikel (166.221 tegen 40.038): de eenheid is het lid, voor
weergave vouw je op naar het artikel. En 73% van alle
activiteitlocatieaanduidingen draagt kwalificatie **`anders geduid`** — daarop
rangschikken kan, filteren niet.

## 3. Ontwerp

### 3.1 Sla de gok op, niet het gevolg

De enige stap die wij *produceren* is koop_id → werkzaamheid. Alles daarna is
een deterministische join over data die al in de DB staat en die door
bronhouders is gecureerd. Materialiseren we dat mee, dan dupliceren we andermans
waarheid en veroudert die stil bij elke regelingwijziging.

```sql
CREATE TABLE vth.vergunning_werkzaamheid (
  koop_id          TEXT NOT NULL REFERENCES vth.vergunningkennisgeving(koop_id)
                                 ON DELETE CASCADE,
  werkzaamheid_urn TEXT NOT NULL,
  rang             SMALLINT NOT NULL,   -- 1 = beste; de top-N zit hierin
  score            REAL    NOT NULL,
  bewijs           TEXT,                -- de frase die matchte
  bewijs_veld      TEXT,                -- titel | beschrijving | inhoud_tekst
  methode          TEXT NOT NULL,       -- lexicaal | embedding | handmatig
  methode_versie   TEXT NOT NULL,       -- 'lex-v1', 'emb-nomic-v1', ...
  bepaald_op       TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (koop_id, werkzaamheid_urn, methode)
);
CREATE INDEX ON vth.vergunning_werkzaamheid (koop_id, rang);
CREATE INDEX ON vth.vergunning_werkzaamheid (werkzaamheid_urn);
```

Drie dingen zitten daar bewust in:

- **`rang` maakt de top-N een leesbeslissing.** Laad álle kandidaten, kap niet
  af bij het schrijven. De viewer zet `WHERE rang <= 2` en die knop blijft
  draaibaar zonder herberekening. Opslag is triviaal: de dekking loopt van
  80,2% bij rang ≤2 naar 81,4% bij rang ≤10, dus vrijwel geen vergunning heeft
  meer dan een handvol kandidaten.
- **`bewijs` maakt het uitlegbaar.** *"Gevonden op 'oprit bestraten' in de
  bodytekst"* is het verschil tussen een koppeling die iemand kan controleren en
  een die je moet geloven.
- **`methode` scheidt de gok van het oordeel.** Handmatig bevestigde
  koppelingen krijgen `methode='handmatig'`; een herlaadronde van de lexicale
  matcher scopet altijd op `methode` en raakt ze dus niet. Zo overschrijft een
  nieuwe run nooit het labelwerk. Dit is dezelfde discipline als het
  `bron:`-veld in het kennismodel: `gebruiker` boven `agent-inferred`.

### 3.2 De rest is een view

```sql
CREATE VIEW vth.v_vergunning_regeltekst AS
SELECT vw.koop_id, vw.rang, vw.werkzaamheid_urn, vw.methode,
       wa.activiteit_urn, ala.kwalificatie,
       jr.identificatie AS juridische_regel_id,
       te.id AS tekst_element_id, te.element_type, te.eid,
       te.regeling_expression
FROM vth.vergunning_werkzaamheid vw
JOIN vth.vergunningkennisgeving v   ON v.koop_id = vw.koop_id
JOIN i2a.werkzaamheid_activiteit wa ON wa.werkzaamheid_urn = vw.werkzaamheid_urn
                                   AND wa.overheid_ns      = v.bg_overheidscode
JOIN p2p.activiteit_locatieaanduiding ala ON ala.activiteit_id = wa.activiteit_urn
JOIN p2p.juridische_regel jr ON jr.identificatie = ala.juridische_regel_id
JOIN p2p.tekst_element te   ON te.wid = jr.regeltekst_wid
                          AND te.regeling_expression = jr.regeling_expression;
```

> **De laatste join-conditie is niet optioneel.** `tekst_element.wid` is niet
> uniek over regelingen (gaps#G-144). Joinen op `wid` alleen levert artikelen
> uit vreemde regelingen op — dezelfde fan-out-bug die in juni 15 query-sites
> raakte.

**Niet materialiseren.** In de meting leveren 11.485 vergunningen 204.789
tekstrijen op, ~17,8 per stuk. Over alle verleende vergunningen is dat ruim 4
miljoen rijen voor alleen rang 1. Laat de view lui. Wil de viewer een badge met
het aantal artikelen, materialiseer dan die ene telling en niet de bladeren.

### 3.3 Wat we beweren

De koppeling heet **niet** *"de regels waarop dit besluit berust"*. Het besluit
zelf is niet openbaar (gaps#G-70) en komt er ook niet. Wat we tonen is:

> *"Regels die golden voor deze werkzaamheid bij dit bevoegd gezag."*

Die formulering hoort letterlijk in de API-omschrijving en in de viewer te
staan, niet alleen in dit document.

## 4. Fasering

| fase | wat | omvang | blokkeert |
|---|---|---|---|
| 0 | bevoegd gezag als kolom | ½ dag | alles |
| 1 | DDL + view | ½ dag | 2 |
| 2 | matcher als loader + CLI | 2 dagen | 4 |
| 3 | gouden set | 1 dag | publicatie |
| 4 | API-endpoint | 1 dag | 5 |
| 5 | viewer-paneel | 1–2 dagen | — |
| 6 | embeddings (optioneel) | 1 week | — |

### Fase 0 — het bevoegd gezag één keer oplossen

De view leunt op `v.bg_overheidscode`, en die bestaat nog niet; nu doet elke
query een `lower(naam)`-join op `core.bronhouder`. Eén kolom, één
normalisatiestap, gemeten over het hele register:

| bestuurslaag | records | opgelost na prefix-strip |
|---|---:|---:|
| gemeente | 859.168 | 99,6% |
| provincie | 31.162 | 100,0% |
| waterschap | 23.789 | **82,1%** (was 14%) |
| dienst/agentschap · samenwerkingsorgaan · ministerie | 11.210 | 0% |

Het waterschap-gat was puur het voorvoegsel: KOOP schrijft *"Waterschap
Hollandse Delta"*, de bronhouder heet *"Hollandse Delta"*. Strippen van
`^(Waterschap|Hoogheemraadschap( van)?|Wetterskip|Gemeente|Provincie)\s+` lost
het op.

De laatste rij is geen bug maar een echte vraag: onder welk bevoegd gezag valt
een vergunning die een omgevingsdienst namens een gemeente publiceert? Zolang
dat open staat valt 1,2% van het register buiten deze keten, hoe goed de matcher
ook wordt. Zie §7.

**Werk**: kolom `bg_overheidscode TEXT` op `vth.vergunningkennisgeving` +
resolutie in `koop_vergunning.py` bij ingest + eenmalige backfill.

### Fase 1 — DDL en view

Tabel en view uit §3 naar `src/ddl.py`, plus een migratiescript in
`scripts/` volgens de bestaande conventie. Idempotent, draaibaar op een
bestaande DB.

### Fase 2 — de matcher als echte loader

Het meetscript wordt een loader: `src/loaders/vergunning_werkzaamheid.py`, met
een CLI-commando `koppel-vergunning-werkzaamheid` naast de bestaande
`laad-werkzaamheid-koppelingen`. De vorm van de matcher staat vast en is
gemeten:

1. Frasetabel uit `i2a.werkzaamheid` (`naam` + `trefwoorden`, ≥4 tekens) —
   6.486 frasen over 294 werkzaamheden.
2. Alleen **discriminerende** frasen: een frase telt mee als ze bij hoogstens
   **2** werkzaamheden hoort. Zonder die grens rust 94,7% van de beste treffers
   op een kaal werkwoord (`verwijderen` hangt aan 12 werkzaamheden) en is de
   keuze een muntworp.
3. N-grammen 1–3 over titel + beschrijving + `left(inhoud_tekst, 4000)`.
4. Snijden op het eigen bevoegd gezag via `i2a.werkzaamheid_activiteit`.
5. Score = `nwoorden*100 + min(nchar,99)`; `rang` via `row_number()`.
6. Alle kandidaten wegschrijven, `methode='lexicaal'`, `methode_versie='lex-v1'`.

Idempotentie: per run `DELETE ... WHERE methode='lexicaal'` voor de behandelde
`koop_id`'s, daarna insert. Handmatige rijen blijven staan.

Incrementeel: draaien op records zonder rij voor de huidige `methode_versie`,
zodat de dagelijkse KOOP-load automatisch meeloopt.

### Fase 3 — de gouden set

**Dit is de enige echte blocker voor publicatie.** Alle percentages hierboven
zijn *bereikcijfers*: ze tellen of een vergunning bij een regeltekst uitkomt,
niet of het de júiste is. Daar is geen enkele meting voor, en de les uit de
categorie-indeling is scherp genoeg om hem niet te herhalen — daar was 45% van
de toewijzingen een muntworp die als feit werd getoond.

Concreet: 150 willekeurige verleende vergunningen, gestratificeerd over
`activiteit_code`, met de hand beoordeeld op drie waarden — *juist*,
*plausibel*, *fout* — en weggeschreven als `methode='handmatig'`. Dat levert in
één dag een precisiecijfer per rang, en daarmee een onderbouwde keuze of je
rang 2 überhaupt wilt tonen.

Zonder dit cijfer bouwen mag; publiceren niet.

### Fase 4 — API

Nieuw endpoint in `ocd-api/vergunningen.py` (router `/v1/vergunningen`):

```
GET /v1/vergunningen/{koop_id}/regels?max_rang=2
```

Respons per kandidaat: werkzaamheid (urn + naam), activiteit, `rang`, `score`,
`bewijs`, en de regelteksten gegroepeerd per artikel met hun leden en
kwalificatie. Plus een booleaans `heeft_regelkoppeling` op het bestaande
lijst-endpoint, zodat de viewer kan filteren zonder N+1.

Let op de `statement_timeout`: de view is lui en de fan-out is groot. Eén
vergunning is goedkoop, een lijst van 50 niet — vandaar de vlag in plaats van de
inhoud op het lijst-endpoint.

### Fase 5 — viewer

Sectie in het detailpaneel van `omgevingsvergunning-register.nl`: *"Regels die
golden voor deze werkzaamheid"*, met per artikel het opschrift, de leden en een
link naar Regels op de kaart. De deeplink-conventie staat vast: encoding op het
FRBR-**work**, anker op artikel-eId (lid-eId doet niets).

Toon `bewijs` als een subtiele herkomstregel. Bij rang > 1 een expliciete
"mogelijk ook"-kop — twee kandidaten naast elkaar is eerlijk, twee kandidaten
als één antwoord is dat niet.

### Fase 6 — embeddings, alleen als de gouden set daarom vraagt

Nomic staat lokaal en `v2a` heeft de infrastructuur. Maar embeddings verhogen
**recall**, en het is nog onbekend of recall of precisie de bottleneck is. Als
fase 3 laat zien dat de lexicale matcher vaak het verkeerde raadt, helpt meer
recall niet — dan moet de score beter. Eerst meten, dan bouwen.

## 5. Risico's

| risico | omvang | wat we doen |
|---|---|---|
| **Precisie onbekend** | alle 57,8% | fase 3; niet publiceren zonder |
| `anders geduid` domineert | 73% van de ALA's | rangschikken, niet filteren |
| Geen bronhouder voor omgevingsdiensten | 1,2% van het register | expliciet buiten scope, §7 |
| Regelingversies: we hebben alleen de actuele | 31% van de works heeft ≥2 versies | claim luidt "zoals ze nú gelden"; 69% van de works heeft maar één versie, dus de vraag is voor twee derde leeg |
| Fan-out van de view | ~4M rijen bij rang 1 | niet materialiseren; vlag op de lijst |
| `wid` niet uniek over regelingen | G-144 | altijd mee joinen op `regeling_expression` |

## 6. Wat we bewust laten liggen

- **De BOPA-afwijkvraag.** Sinds de parser-fix van 2026-09-20 hebben alle 16.512
  BOPA's hun vergunningsgebied in `geometrie_rd`. *Waarvan* ze afwijken is
  echter geen activiteit-vraag: een eerste toets op 10 vergunningsgebieden gaf
  1 van de 10 raak zodra je ambtsgebied-locaties uitsluit. Dat antwoord zit
  vermoedelijk in de functie-/gebiedsaanwijzing-laag. Eigen traject, eigen
  meting.
- **De relatie-typering** `vergund_op_grond_van` versus `afgeweken_van`. Het
  signaal ligt er (`afwijk_status` + de 1:1-join op `omgevingsvergunning_dso`),
  maar zonder het vorige punt is het een label zonder inhoud.
- **Andere besluittypen dan `verleend`.** Aanvragen (51,5% van het register)
  zouden dezelfde keten kunnen lopen, maar "welke regels gólden" is bij een
  aanvraag een ander verhaal dan bij een besluit.

## 7. Open besluiten

1. **Top-2 of top-1 als default in de viewer?** De meting zegt top-2 (+21pp voor
   twee extra schermregels), maar dat hangt af van de precisie uit fase 3.
2. **Wat doen we met omgevingsdiensten?** Mappen op de opdrachtgevende gemeente
   (hoe?), of eerlijk buiten de keten laten?
3. **Draait dit ook op productie**, of blijft het lokaal tot de gouden set er
   is? Aanbeveling: lokaal tot na fase 3.
