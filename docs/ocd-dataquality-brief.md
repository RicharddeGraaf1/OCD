# Prompt — OCD datakwaliteit: fixes toelichten + wat te onderzoeken/fiksen

> Plak dit als opdracht in een nieuwe sessie met toegang tot de OCD-repo.
> Doel: **grip krijgen op de datafouten** in de OCD-database en zorgen dat ze de
> metingen niet meer stilletjes vervuilen.

## Aanleiding
Tijdens een traject (semantische-index-PoC voor de Omgevingsbot) bleken meerdere
data-/loaderfouten de metingen te **obfusceren** — elk pas zichtbaar na
handmatig graven. Het patroon: een fout (verwisselde provincie, gefaald document,
leeg punt) bederft stil een meting tot iemand het toevallig ontdekt. Dat moet
beheersbaar worden.

## Verbinden
- DB = Postgres in Docker-container `dso-postgis`, `localhost:5434/dso`.
- Via `from src.db import get_conn` (in `c:/GIT/OCD/dso-loader`) of de OCD-API db.
- **Read-only tijdens onderzoek; writes pas na akkoord; raak `overheidscode` (PK) nooit aan.**

## Wat al gevonden + gefixt is (NIET opnieuw doen)
1. **`bronhouder.naam`-misalignment** — pv21 stond op "Groningen" maar is
   **Fryslân**; pv25 "Flevoland"→**Gelderland**; pv30 "Gelderland"→**Noord-Brabant**;
   mnre1130 code-only→**Ministerie van IenW**. Alle vier gefixt (UPDATE). **Root**:
   de naam komt van handmatige CLI-invoer (`load-ow --overheid 'pvXX,Naam'`) +
   `ON CONFLICT DO NOTHING`, dus een typefout blijft plakken. **Impact was ernstig**:
   een query op "Provincie Gelderland" mapte naar pv30 = Noord-Brabant → fout antwoord.
2. **Waterschap-code-mismatch** — de echte Wetterskip Fryslân-waterschapsverordening
   publiceert onder **`ws0653`** (was code-only gelabeld), niet `ws0680` (die heet
   "Wetterskip Fryslan" maar heeft géén WV). ws0653-WV is nu geladen + gelabeld.
   Status van `ws0680` blijft onduidelijk (stale/duplicaat?).
3. **STOP-parser-bug** — `dso-loader/src/parsers/stop_xml.py` `_walk` crashte op
   XML-commentaarnodes (`Comment.tag` is geen string → `etree.QName` faalt).
   Gefixt (non-string tags overslaan). **Dit blokkeerde het laden van elk document
   met XML-commentaar.**

## Te onderzoeken / fiksen (geprioriteerd)

### 1. Volledige bronhouder-integriteit — HOOG
- Voer de aparte audit uit: `docs/bronhouder-audit-prompt.md` (alle provincies/
  waterschappen/rijk/gemeenten code↔naam tegen regeling-opschrift; duplicaten en
  code-only labels; stale codes zoals `ws0680`).
- **Structurele fix**: canonieke code→naam-map (provincies + rijk) in de
  bronhouder-insert (`ow_loader.load_ow_overheid` / `cli.py load-ow`), met
  `ON CONFLICT … DO UPDATE SET naam` voor die gecontroleerde lagen — zodat
  handmatige typefouten niet meer blijven plakken.

### 2. Load-volledigheid na de parser-fix — HOOG
- De parser-bug betekent dat documenten met XML-commentaar eerder **niet of half**
  geladen kunnen zijn. **Onderzoek: welke regelingen zijn gefaald of incompleet
  geladen?** Is er een load-error-log? Re-load de getroffen regelingen nu de parser
  gefixt is.
- **Voeg load-status per regeling toe** (geladen / gefaald / partieel) als die er
  niet is — anders blijven stille load-fouten onzichtbaar.
- Hergebruik `scripts/diff_dso_bronhouder_coverage.py`: welke regelingen in de DSO
  zitten niet in `p2p`?

### 3. Geo-/punt-dekking — HOOG (dit obfusceerde een meting direct)
- In de retrieval-eval gaven sommige punten **`hits=0`** omdat de geo-scope een
  regeling teruggaf die niet (volledig) geladen was (bv. een subsidie-regeling in
  Flevoland). **Onderzoek: voor een steekproef van punten — leveren álle
  geo-intersecterende regelingen ook daadwerkelijk `tekst_elementen`?** Kwantificeer
  het gat tussen "`locatie_subdiv` intersect → regeling" en "regeling heeft inhoud".

### 4. Annotatiedichtheid zichtbaar maken — MIDDEN
- De structurele annotatie is grotendeels hol (≈95% op ambtsgebied-schaal, ≈90%
  kwalificatie "anders geduid" — dit is grotendeels **content-realiteit** van de
  bruidsschat, niet per se een bug). Maak dit een **staande rapportage**
  (locatie-discriminatie-index, kwalificatie-specificiteit, artikel-dekkingsgraad
  per bronhouder) zodat een lage retrieval-score herkenbaar is als content-realiteit
  i.p.v. een bug. (Zie de annotatiedichtheid-analyse in de OmgevingswetKnowledgeBase-vault.)

### 5. Een datakwaliteit-/health-laag — DE KERN VAN HET VERZOEK
- Bouw een **data-health-view/-endpoint** dat in één oogopslag per bronhouder/
  regeling toont: load-status, naam-integriteit, annotatiedichtheid, en punt-dekking.
  Dan zie je dataproblemen **vóór** ze een meting vervuilen, in plaats van erna.

## Deliverable
1. **Datakwaliteit-rapport** per categorie (bronhouder-integriteit, load-volledigheid,
   geo-dekking, annotatiedichtheid) met bevindingen + omvang.
2. **Concrete fixes** (SQL / loader-patches) — eerst tonen, uitvoeren na akkoord.
3. Voorstel voor een **staande data-health-rapportage** zodat de gebruiker grip houdt.

## Leidend principe
Het doel is **grip + niet-obfusceerde metingen**, niet perfecte data. Elke fix moet
het onderscheid scherper maken tussen "de aanpak werkt niet" en "de data klopt niet"
— want dat onderscheid ging deze sessie telkens verloren.
