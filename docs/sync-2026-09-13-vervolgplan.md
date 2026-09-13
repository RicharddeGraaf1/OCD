# Vervolgplan — de bevindingen van de sync van 12/13 september

*Opgesteld 2026-09-13. Bronnen: [sync-2026-09-13-bevindingen.md](sync-2026-09-13-bevindingen.md)
(14 bevindingen), [enrich-koop-tempo-analyse.md](enrich-koop-tempo-analyse.md),
[werkbank-stabiliteit.md](werkbank-stabiliteit.md), en vault `gaps.md` G-149 t/m G-151.*

Al gedaan en gecommit (`c4d2b67`): de `ANNOTATIE_FOUTEN`-fix, de noodrem op een
weggevallen database, `p2p.divisie` in de replicatielijst, de FK op
`v2a.artikel_indeling` en de vrijetekst-backfill naar prod. Dit plan gaat over
wat er ná die commit nog openstaat.

---

## De ordening

Niet op grootte maar op één vraag: **wat verbergt op dit moment fouten?** Dat
is de rode draad van deze hele run. Twee controles bleken maandenlang stil te
falen, en beide keren was het gevolg niet "iets ging kapot" maar "niemand kon
zien dat het kapot was". Werk dat de zichtbaarheid herstelt gaat dus vóór werk
dat tijd bespaart, hoe verleidelijk die tijdwinst ook is.

| Golf | Thema | Doorlooptijd | Waarom deze volgorde |
|---|---|---|---|
| 1 | Blinde vlekken dichten | ~1 dag | Hier zit het risico dat we hetzelfde nog eens missen |
| 2 | Kleine fixes met een bewezen oorzaak | ~2 uur | Goedkoop, elk uit een concrete fout van deze run |
| 3 | De drie onverklaarde verschillen | ~halve dag | Onverklaard is de toestand waarin een controle ophoudt gelezen te worden |
| 4 | Tijdwinst in `enrich-koop` | ~halve dag | De grootste besparing, maar meet-eerst |
| 5 | De LLM-transitie | apart traject | Vraagt eerst een keuze van jou (G-151) |

---

## Golf 1 — blinde vlekken dichten

### 1.1 Schema-diff naast de rij-diff *(G-150 — het belangrijkste punt van dit plan)*

`diff_lokaal_prod.py` telt rijen per tabel. Daardoor kon `v2a.artikel_indeling`
op prod **volledig zonder foreign key** staan terwijl lokaal een gevalideerde
`ON DELETE CASCADE` hangt — maandenlang, met 72 spookrijen als gevolg en een
teller die elke sync oploopt. De kolomdrift-controle in
`repliceer_p2p_naar_prod.py` ving dit niet: die kijkt alleen naar de 28 tabellen
die hij kopieert, en dat is toeval en geen systeem.

**Doen**: constraints, indexen en kolommen per tabel aan beide kanten
vergelijken, met dezelfde verwachtingen-mechaniek als de rij-diff. Draai hem
één keer over het hele schema voordat je iets repareert — er is geen reden aan
te nemen dat die FK de enige was.

> **Reken op vondsten.** Dit is het soort controle dat bij zijn eerste run
> meteen iets vindt; dat gebeurde ook met de tabelbrede diff zelf (die vond
> `kaartlaag`, `pons` en 660k achterstallige tegelrijen). Plan er tijd voor
> ná de eerste run, niet alleen ervóór.

### 1.2 Voortgang van `enrich-koop` zichtbaar maken

De stap schrijft anderhalf uur lang geen enkele regel naar het sync-log. Van
buitenaf is "loopt nog" niet te onderscheiden van "hangt", en ik had er zelf
bijna een verkeerde conclusie aan verbonden.

**Doen**: een teller per N records naar stdout, zodat tempo én een
throttle-inzinking zichtbaar zijn terwijl de stap loopt. Neem in het runbook
ook de DB-check op — met de waarschuwing dat `inhoud_geladen_at` UTC is.

### 1.3 Runbook-correcties

| Plek | Wat er niet klopt |
|---|---|
| Leerpunt 0 | `python … > log 2>&1; echo "exit=$?"` heeft dezelfde val als een pipe zodra je hem naar de achtergrond stuurt: de taak rapporteert de exitcode van `echo`, dus 0. Zo kwam de crash van 12-09 binnen als "geslaagd" |
| Stap 6, controlequery | `USING (regeling_expression)` terwijl de CTE `frbr_expression` levert — de query draait niet |
| Stap 5 | Redeneert op 1.238.206 `uitvoeringsregel`; die telling bevatte dubbelen die commit `282f1ab` heeft opgeruimd. Nu 490.849 |
| Stap 3, generalisatie | "1,8 min voor tien bronhouders" gold voor tien kleine. Met twee provincies erbij: 46,6 min. Druk de verwachting uit per bronhoudergrootte |
| Stap 3, prod-herbouw | Voeg toe: controleer met `SELECT current_database()` dát je op prod zit. `OCD_DB_URL` leeg laten faalt stil en herbouwt lokaal |

---

## Golf 2 — kleine fixes met een bewezen oorzaak

Elk hiervan komt uit een concrete storing van deze run. Samen ongeveer twee uur.

1. **`lib.http_get` retryt geen socket-timeout** (§12). De 429/503-backoff is
   netjes, maar de `except` vangt alleen `HTTPError`; een `TimeoutError` brak de
   MER-harvest af op pagina 1250 van 2266. Neem `TimeoutError` en
   `urllib.error.URLError` mee in dezelfde lus.
   *Dit is de spiegel van de noodrem: daar ging het om te lang doorgaan met een
   fout die niet overgaat, hier om te vroeg stoppen bij een fout die dat wel doet.*
2. **`vul_locatie_generalisatie.py` valt om op zijn eigen `→`** zodra stdout
   naar een bestand gaat (§5). `sys.stdout.reconfigure(encoding="utf-8")` bij
   het opstarten. Andere scripts met rich-uitvoer hebben dit waarschijnlijk ook.
3. **`test_imtr_peildatum` telt 3 call-sites, het zijn er 2** sinds commit
   `590b2a0` het `activiteitKoppelingen`-blok verving (§8). Getal aanpassen,
   met een regel erbij waarom het er drie waren.

---

## Golf 3 — de drie onverklaarde verschillen *(G-149)*

| tabel | verschil | wat al is uitgesloten |
|---|---|---|
| `p2p.kaartlaag` | prod mist 170 | geen wezen aan beide kanten; de ouder-`kaart` bestáát op prod |
| `p2p.tekstdeel` | prod heeft 11 méér | geen wezen aan beide kanten |
| `v2a.pad_categorie` | prod heeft 7 méér | — |

Alle drie bestonden vóór deze sync. Per tabel is de vraag dezelfde: is dit een
scope-gat in de replicatie (zoals `p2p.divisie` bleek) of legitieme drift?

**Doen**: uitzoeken, en dan óf repareren óf **met reden** in
`diff_verwachtingen.yml` zetten. Niet laten staan als "onverklaard" — dat is
precies hoe een controle ophoudt gelezen te worden.

`p2p.kaartlaag` eerst: die voedt de kaartlagen in de viewer, dus 170 ontbrekende
rijen zijn potentieel zichtbaar voor bezoekers.

**Losse onderzoeksvraag uit dezelfde hoek** (§10): de Gebiedengroep
*"Raadhuislaan 1K"* van gm0828 wordt door geen enkele tabel aangewezen, terwijl
de regeling die deze run binnenkwam het *Programma Raadhuiskwartier* van
diezelfde gemeente is. Dat ziet er niet uit als toeval maar als een ontbrekende
koppeling aan de laadkant. Niet onderzocht.

---

## Golf 4 — `enrich-koop` van 2,2 uur naar (waarschijnlijk) ~1 uur

De volledige meting staat in [enrich-koop-tempo-analyse.md](enrich-koop-tempo-analyse.md).
Kort: fetch 30 ms, parse 0,3 ms, database 60/60 metingen idle-wachtend, proces
0,3% van een kern. De 1,2 s per record is KOOP-throttling (HTTP 429) die als
netwerkstoring wordt behandeld en met een **vaste backoff van 5 s** wordt
beantwoord. De enricher gaat te hard, wordt teruggefloten, slaapt vijf seconden,
gaat weer te hard — en middelt uit op 0,83 req/s.

**Stap 1 (meten, ~1 uur):** een oplopende probe — 1,0 / 1,5 / 2,0 / 2,5 req/s,
elk twee minuten, tel de 429's. De limiet ligt aantoonbaar tussen 0,83 en
3,3 req/s; waar precies is niet gemeten.

**Stap 2 (bouwen):** 429 apart van echte storingen afhandelen, `Retry-After`
honoreren als KOOP die stuurt, en het interval adaptief regelen (AIMD) in plaats
van vast.

**Wat níét helpt** — en dat is de moeite van het opschrijven waard, want het
waren allebei plausibele ideeën: parallelliseren (de fetch is 30 ms, er valt
geen latency te verbergen — je loopt alleen sneller tegen dezelfde limiet) en de
XML lokaal cachen (parsen kost 0,3 ms, en de XML stáát al in `inhoud_xml`).

**Onderzoeken, niet aannemen:** of KOOP een bulk- of dagexport aanbiedt. Elke
publicatie is nu een eigen FRBR-resource; of daarnaast een bulkroute bestaat is
niet nagekeken.

---

## Golf 5 — de LLM-transitie *(§11 / G-151)*

**Dit begint met een keuze van jou**, niet met code. De regel "niets op een
lokaal model" is niet naleefbaar voor de embed-kant: Anthropic levert geen
embeddings-API, en het bevraagmodel moet exact het indexmodel zijn. De index is
`nomic-embed-text` (768d) met 1.653.158 vectoren.

| variant | wat het betekent |
|---|---|
| **1. "geen lokaal *taal*model"** *(aanbevolen)* | Embeddings blijven lokaal, alle generatie en oordelen op Sonnet. Dekt wat je in de praktijk raakt: de kwaliteit van antwoorden. Kleinste ingreep |
| 2. Gehoste embeddingprovider | Voyage/OpenAI/Cohere, mét bake-off vooraf. Volledige herbouw van 1,65 mln vectoren; opnieuw wisselen kost hetzelfde |
| 3. Retrieval zonder vectoren | Valt af — de vectorlaag dekt juist de norm-as waar SKOS blind is |

**Zodra de keuze er is** (en variant 1 verandert er weinig aan):

- `ocd-api/llm.py`: `OCD_LLM_PROVIDER` van `ollama` af, en `claude-sonnet-4-6`
  → `claude-sonnet-5`. Geen stille terugval bij een ontbrekende sleutel.
- Losse generatie-scripts (`begrijpelijk-*`, `classify_wijziging`,
  `2026-07-repair-lege-elementen`) op dezelfde helper.
- **Houd twee gevallen apart.** Batchwerk in een sync gaat via subagent-fan-out
  op het abonnement — die route bestaat al en werkte deze run voor 2.337
  oordelen. Runtime op productie kan dat niet en heeft de anthropic-tak (dus
  tegoed) of Groq nodig. Eén regel dekt die twee niet met dezelfde mechaniek.

---

## Buiten dit plan, bij jou

- **Microsoft Store: app-updates uit.** Zonder dat kan dezelfde WSL-update je
  volgende nachtsync opnieuw omleggen. Gewoonte erbij: `wsl --update` bewust
  vóór een sync draaien.
- **Docker Desktop: AutoStart aan** (staat nu op `False`).

Beide staan met verificatiecommando's in [werkbank-stabiliteit.md](werkbank-stabiliteit.md).

---

## Wat ik zou doen als er maar één dag is

Golf 1.1 (de schema-diff) en golf 2. Die twee samen halen weg wat deze run het
duidelijkst blootlegde: niet dat er dingen misgaan — dat gebeurt in elke
pijplijn — maar dat ze misgingen zonder dat iets het zei. De tijdwinst uit
golf 4 is verleidelijker, maar een snellere sync die stil faalt is een
verslechtering.
