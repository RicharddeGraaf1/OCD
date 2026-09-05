# Plan: de leerpunten van sync 2026-09-04 wegwerken

*Opgesteld 2026-09-05. Hoort bij [sync-2026-09-04-leerpunten.md](sync-2026-09-04-leerpunten.md).
Dat document beschrijft wát er misging; dit document zegt in welke volgorde we het
oplossen en waarom die volgorde zo is.*

---

## De ordening

Niet op ernst en niet op moeite, maar op **wat een volgende sync stil kan laten
mislukken**. Een punt dat alleen tijd kost mag wachten; een punt dat data
onzichtbaar laat verdwijnen niet.

Drie groepen:

| | | |
|---|---|---|
| **A. Detectie** | 3, 8, 10 + de 133 | We meten de verkeerde dingen. Zolang dit niet klopt, weten we van de volgende sync niet of hij geslaagd is |
| **B. Vorm** | 6, 7, 5 | Stappen die alleen werken via één toevallige route, of die falen als succes rapporteren |
| **C. Documentatie** | 2 | Een reden in `diff_verwachtingen.yml` die een echt gat vrijpleit |
| **D. Los** | 13 | Productie-endpoint, hangt aan Railway-logs, staat verder los |

Punt 1, 4, 9, 11 en 12 zijn klaar. Punt 1 is op 2026-09-05 afgemaakt: de
replicatiescope gebruikt nu `p2p.tekstdeel.regeling_expression`.

---

## A. Detectie — eerst, want hierop rust al het andere

### A1. Inhoudscontrole naast de telling (punt 3)

**Waarom eerst.** `diff_lokaal_prod.py` is het enige instrument dat de sync
achteraf toetst, en het heeft een blinde vlek die we nu gemeten hebben: acht
locaties met dezelfde sleutel en een andere geometrie waren voor hem gelijk. Elke
volgende conclusie ("0 afwijkend") is precies zoveel waard als deze controle.

**Wat.** Per bronhouder één hash in plaats van per rij een vergelijking:

```sql
SELECT substring(identificatie from 'nl\.imow-([a-z0-9]+)\.') AS bh,
       md5(string_agg(md5(ST_AsBinary(geometrie)), '' ORDER BY identificatie))
  FROM p2p.locatie WHERE geometrie IS NOT NULL GROUP BY 1
```

382 regels aan elke kant, verschil per bronhouder zichtbaar. Pas bij een
afwijkende bronhouder de rijen ophalen.

**Afbakening.** Alleen `p2p.locatie.geometrie` — dat is waar drift is
aangetoond en waar hij doorwerkt in subdiv, generalisatie en dus de kaart.
Niet uitbreiden naar alle tabellen voordat deze zich bewezen heeft.

**Klaar als.** `diff_lokaal_prod.py` meldt geometrie-drift, en een kunstmatig
gewijzigde geometrie op prod wordt gevonden. *Halve dag.*

### A2. Drempel en ruisband voor de afgeleide lagen (punt 8)

**Waarom hier.** Zonder drempel kost de volgende inhaalslag weer uren aan
bronhouders waar niets mis is, en zonder ruisband blijft de diff melden over
PostGIS-versieverschil — een controle die ruis geeft wordt niet gelezen.

**Wat.**
1. In de herbouwstap: alleen een bronhouder herbouwen bij `|verschil| > 500`
   **of** `> 1%` van zijn omvang. Gemeten 2026-09-04: dat had ~80 bronhouders en
   ruim een uur bespaard zonder één rij echt gat te laten staan.
2. In `diff_verwachtingen.yml`: de band als **per-bronhouder**-marge vastleggen,
   met de PostGIS-versies (lokaal 3.5/PG16, prod 3.7/PG17) als reden. Een
   tweezijdig verschil van tientallen is dan zichtbaar géén signaal — herkenbaar
   omdat prod er soms bóven staat.

**Klaar als.** Een herbouwronde op de huidige stand raakt nul bronhouders.
*Halve dag.*

### A3. De preview eerlijk maken over de vth-verrijking (punt 10)

**Wat.** De schatting baseren op `te laden kennisgevingen + bestaande
achterstand` in plaats van alleen de achterstand, en het tempo uit de laatste
drie runs halen in plaats van uit de constante 4/s.

**Los daarvan te onderzoeken.** Het tempo zakte van 1,7/s (run 11) naar 0,9/s
(run 12) — een halvering die niemand verklaart. Eerst meten of dat KOOP-zijdig
is of van ons, vóór er iets aan geoptimaliseerd wordt.

**Klaar als.** De preview voorspelde de duur van de volgende run binnen een
factor twee. *Twee uur, plus een aparte meting voor de trendbreuk.*

### A4. De 133 schiften (G-144-restant)

**Volgorde-afhankelijk**: kan pas ná A5 hieronder, want de meting is vertekend
zolang `p2p.tekstdeel.regeling_expression` op 82,8% staat — een regeling waarvan
de tekstdelen bestaan maar niet gemapt zijn, lijkt ten onrechte leeg.

**Wat.** (a) De kolom voller krijgen: de 4.796 onbekende horen bij expressies
waarvan de ZIP is overschreven. Eerst **tellen om hoeveel documenten dat gaat**
voordat er iets opgehaald wordt — mogelijk zijn het er weinig en is een
Presenteren-call per document genoeg. (b) Daarna de detectiequery opnieuw; wat er
dan nog staat met een regelstructuur-documenttype is per definitie fout.
(c) Die query als vaste controle in het sync-rapport, naast de bestaande
indeling-controles: hij vindt precies de klasse die G-144 was.

**Klaar als.** De lijst bevat geen enkel regelstructuur-document meer, en het
sync-rapport meldt hem elke run. *Een dag.*

---

## B. Vorm — kleine ingrepen, groot rendement

### B1. `sys.path`-bootstrap in elk script (punt 6)

Eén regel bovenaan elk bestand in `scripts/`:

```python
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
```

`vul_locatie_generalisatie.py` heeft hem nodig (viel om op `No module named
'src'`); `refresh_drieslag.py` doet nu `sys.path.insert(0, ".")` en is daarmee
cwd-afhankelijk — dat is dezelfde val met een ander gezicht. `backfill_tekstdeel_regeling.py`
heeft hem al.

Dit maakt de commando's in het runbook wáár in plaats van het runbook aan te
passen aan een beperking. *Een uur, inclusief langslopen van alle scripts.*

### B2. `psql`-aanroepen vervangen (punt 7, tweede helft)

Het runbook schrijft op drie plekken een kale `psql` voor die op geen enkele PATH
staat: de health-MV's in stap 3, `verversen.sql` in 6b en de controlequery's in
stap 7. De PowerShell-scripts lossen het zelf op met een expliciet `$PgBin`-pad,
`instructieregels.nl` gebruikt `docker exec`. De losse commando's zijn dus nooit
uitgevoerd zoals ze er staan.

Vervangen door psycopg-equivalenten (zoals op 05-09 voor de health-MV's is
gedaan) of door dezelfde `$PgBin`-constructie. *Twee uur.*

### B3. `pipefail` en exitcodes (punt 7, eerste helft)

`set -o pipefail` in elke sync-shellwrapper, en in het runbook expliciet: schrijf
de uitvoer naar een bestand en echo de exitcode apart, in plaats van door een
`| tail` te halen. Ik ben hier na het opschrijven nog twee keer in getrapt — het
is geen kennisprobleem maar een vormprobleem. *Een uur.*

### B4. Migratie-ledger (punt 5)

Een tabel `core.migratie` (bestandsnaam, tijdstip, doelwit-DB) die elke
`scripts/*.sql` bij toepassing registreert, plus een controle in
`diff_lokaal_prod.py`: **welke migraties mist prod?** Dan is dat een query in
plaats van een herinnering.

Aanleiding: prod miste alle drie de indexen uit
`2026-09-add-generalisatie-prefix-index.sql`, en op 05-09 opnieuw de
tekstdeel-kolom — die werd gelukkig gevangen door de kolomdrift-controle van het
replicatiescript, maar dat is toeval en geen systeem.

Dit is §3a ("een backfill levert zijn eigen pad naar prod mee") toegepast op DDL.
*Een dag, want de bestaande migraties moeten met terugwerkende kracht
geregistreerd worden.*

---

## C. De onjuiste reden repareren (punt 2)

In `diff_verwachtingen.yml` staat bij `p2p.locatie` dat een ongerefereerde
locatie toch niet getoond wordt. Dat klopt niet: `locatie_subdiv` wordt per
bronhouder over **álle** polygoonlocaties gebouwd, `locatie_generalisatie` is
daarvan afgeleid, en `tiles.py` leest dat. De 115 pv28-locaties waren goed voor
72.262 subdiv-stukjes.

**Wat.** De reden herschrijven met die keten erin, en de conclusie omdraaien:
`p2p.locatie` hoort **integraal** gespiegeld te worden in plaats van via de
scope. Het zijn 321.096 rijen; de sleutelvergelijking kost seconden.

**Waarom dit apart staat en niet triviaal is.** Een verkeerde reden in dat
bestand is erger dan geen reden: hij pleit een echt gat vrij en zorgt dat niemand
er nog naar kijkt. *Twee uur.*

---

## D. `/v1/load-status` (punt 13)

Staat los van de rest en hangt op informatie die we nu niet hebben.

1. Railway-logs van de `ocd-api`-service ophalen tijdens een call — dat geeft de
   traceback in één keer. Alles aan de databasekant is al uitgesloten: elke query
   die het endpoint draait slaagt, de hele reeks in 12,7 s, en het ingebouwde
   vangnet doet aantoonbaar zijn werk.
2. Los daarvan: `bron_totaal()` doet `count(*)` op een tabel van 1,43 GB —
   **35,6 s** op een container met 512 MB `shared_buffers`. Een
   `reltuples`-schatting uit `pg_class` is voor een actualiteitsdashboard ruim
   nauwkeurig genoeg en kost microseconden. Dat is de moeite waard ongeacht de
   500.
3. Nagaan of het dashboard dat dit endpoint voedt eigenlijk gemist wordt. Als die
   pagina al maanden leeg is zonder dat iemand het merkte, is dat op zichzelf een
   antwoord — en dan is optie 2 het enige dat overblijft.

*Onbekend tot de logs er zijn; de `reltuples`-ingreep is een uur.*

---

## Volgorde in één regel

**A1 → A2 → B1 → B3 → C → A3 → B4 → A5/A4 → D**

Detectie eerst, want zonder betrouwbare meting weet je van elke volgende
reparatie niet of hij werkte. Dan de goedkope vormingrepen die verdere stille
mislukkingen voorkomen. De documentatie-reparatie (C) vroeg, omdat een onjuiste
reden actief schade doet. Het zware werk (ledger, de 133) daarna, en D wanneer de
logs beschikbaar zijn.

## Wat níét in dit plan staat

- **Punt 11** (SQL splitsen op `;`) was mijn eigen werkwijze; daar valt in de
  repo niets aan te repareren.
- **Een grote refactor van de replicatiescope.** Punt 1 is opgelost met één
  extra tak, niet met een herontwerp. De scope is complex omdat het domein dat is
  — IMOW-objecten zijn gedeeld en hangen aan verschillende ankers — en een
  herontwerp zonder aanleiding zou de bewezen paden opnieuw in gevaar brengen.
- **Automatisering van stap 3.** Dat staat al als openstaand punt in §5 van het
  runbook en verdient een eigen afweging; het hoort niet bij deze leerpunten.
