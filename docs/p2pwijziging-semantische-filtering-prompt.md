# Prompt — Semantische filtering van p2pwijziging-wijzigingen met een vector-DB

> Plak dit als opdracht in een nieuwe sessie met toegang tot de OCD-repo.

## Probleem
`p2pwijziging` (ontwerpen + besluitversies, delta op `p2p`) levert **veel te veel
wijzigingen**. Elke renvooi-delta — gewijzigde `tekst_element`, `annotatie_delta`,
`locatie_delta` — telt als "wijziging", maar het overgrote deel is **triviaal/ruis**
(hernummering, formatting, metadata, whitespace, boilerplate) terwijl maar een klein
deel **inhoudelijk betekenisvol** is. Het volume is onbruikbaar groot.

## Idee
Gebruik **embeddings (vector-DB)** om per wijziging de **semantische afstand** tussen
de **oude** (`p2p`) en **nieuwe** (`p2pwijziging`) tekst te bepalen:
- kleine afstand → triviaal → **collapse/verberg**;
- grote afstand → betekenisvol → **surface**, gerangschikt op afstand (grootste
  inhoudelijke verandering eerst).

Bonus: embeddings **clusteren identieke boilerplate-wijzigingen over regelingen heen**
(één keer tonen i.p.v. honderden keren) → nog meer volumereductie.

## Herbruik bestaande infra (NIET opnieuw uitvinden)
Er staat al een werkende embedding-pijplijn van het Omgevingsbot-traject:
- **pgvector** in schema `v2a` (tabel `v2a.tekst_embedding`, `vector(768)` + HNSW);
- embedding-model **`nomic-embed-text`** via Ollama, **batch via `/api/embed`**
  (~38 ms/eenheid op GPU; veel sneller dan losse calls);
- eenheid = Lid/Divisietekst met **kop-pad-prefix** voor context.
Zie `omgevingsbot.nl/backend/tests/evaluation/` + de vault-analyse
`Plan semantische index A voor schaalbare regelretrieval.md`. Hergebruik dit patroon.

## Aanpak (voorstel)
1. **Koppel oud↔nieuw**: voor elke `p2pwijziging.tekst_element` met renvooi, vind het
   corresponderende `p2p.tekst_element` (zelfde `wId`/`eId` in de regeling-expression).
   - `bewerking='toegevoegd'` → geen oud (pure toevoeging, apart bucket);
   - `bewerking='verwijderd'` → geen nieuw (apart bucket);
   - `bewerking='gewijzigd'` → oud+nieuw paar.
2. **Embed beide** (`inhoud_plain` oud + nieuw) met `nomic-embed-text`.
3. **Cosine-afstand** per paar → een `significantie`-score (1 − cosine).
4. **Drempel/rangschikking** op een gekalibreerde ε:
   - afstand < ε → triviaal → collapse;
   - afstand ≥ ε → betekenisvol → surface, gesorteerd aflopend.
5. **Cluster** de betekenisvolle wijzigingen (vector-clustering, bv. door dezelfde
   embedding-afstand-vector te groeperen) om herhaalde boilerplate samen te vatten.

## Validatie (hoe weet je dat het werkt)
- **Gouden set**: pak ~20–30 wijzigingen, label handmatig "triviaal" vs "betekenisvol".
- Kalibreer **ε** zodat de afstand die twee scheidt; rapporteer precision/recall.
- Meet de **filter-ratio**: welk % van het volume valt als triviaal weg? (verwacht groot.)

## Deliverables
1. Script/query dat per ontwerp/besluitversie de wijzigingen rangschikt op semantische
   significantie (betekenisvol boven, triviaal gecollapst).
2. Gekalibreerde **ε** + de filter-ratio op de gouden set.
3. Voorstel voor landing in de pijplijn/viewer: bv. een `significantie`-kolom op de
   delta-tabel (vullen tijdens de ontwerp-load), of een view die alleen betekenisvolle
   wijzigingen toont.

## KRITISCHE caveat (lees dit eerst)
Embeddings detecteren **semantische gelijkenis**, niet **juridische zwaarte**. Een
**piepkleine** tekstwijziging kan juridisch **kantelend** zijn:
- een toegevoegde/verwijderde **negatie** ("niet", "geen", "tenzij");
- een **numerieke wijziging** (1:10 → 1:100, "5 meter" → "15 meter", "14" → "11");
- een **verwijzing/uitzondering** die omklapt.
Zulke wijzigingen geven een **kleine** embedding-afstand maar zijn juist de
belangrijkste. → **Combineer de vector-filter met een lexicale/structurele vangnet-
heuristiek**: toon ook wijzigingen waar de semantische afstand klein is maar (a) een
negatie/uitzonderingswoord, (b) een getal/eenheid, of (c) een kwalificatie/verwijzing
verandert. Drempel conservatief; liever een paar triviale tonen dan één kantelende missen.

## Uitbreidingen (andere delta-typen)
- **`annotatie_delta`** (JSONB): significantie deels **structureel** — een
  kwalificatie-flip (toegestaan↔vergunningplicht), activiteit- of locatie-wijziging
  is altijd betekenisvol; embeddings alleen voor vrije-tekst-velden.
- **`locatie_delta`** (PostGIS): **geen** embeddings — gebruik geometrische maat
  (oppervlakte-delta / overlap-percentage via PostGIS) als significantie.

## Verbinden
- DB: Postgres in Docker `dso-postgis`, `localhost:5434/dso`. Via
  `from src.db import get_conn` (in `c:/GIT/OCD/dso-loader`) of de OCD-API db.
- Schema: `OCD/SCHEMA-INDELING.md` (§`p2pwijziging`) + `OCD/docs/p2pwijziging.md`
  (besluit/procedurestap/tekst_element-mirror/annotatie_delta/locatie_delta + filter-logica).
- Embedding: lokale Ollama `/api/embed`, model `nomic-embed-text`.

## Leidend principe
Het doel is **volumereductie zonder iets juridisch belangrijks te missen**. De
vector-afstand is de **grove zeef** (haalt de massa boilerplate eruit); de
lexicale/structurele heuristiek is het **fijne vangnet** (vangt de kleine-maar-
kantelende wijzigingen). Meet beide tegen een handmatige gouden set vóór uitrol.
