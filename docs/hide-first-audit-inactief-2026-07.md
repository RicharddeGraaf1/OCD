# Hide-first-audit — `inactief`-gate op alle retrieval-ingangen

*2026-07-28. Onderdeel A van het opschoonplan (zie
`opschoning-verouderde-versies-plan.md` + vault G-95). Doel: bevestigen dat
géén retrieval-pad verdrongen/ingetrokken regelingversies (`p2p.regeling.inactief`)
aan een gebruiker teruggeeft — vóórdat we fysiek prunen op prod.*

**Waarom dit los van de prune moet:** de prune verwijdert alleen
`verouderde-versie`. De **8 `ingetrokken`** regelingen blijven bestaan-maar-inactief,
dus het `NOT inactief`-filter is de blijvende vangnet-laag en moet op élk pad sluiten.

## Scope & methode

Alle DB-retrieval loopt server-side in `ocd-api` (bot en viewer zijn API-consumenten
zonder eigen DB-toegang — geverifieerd). Drie parallelle audits over alle
`.py`-modules die `p2p.regeling` / `p2p.tekst_element` / `p2p.juridische_regel`
kunnen teruggeven. Conservatief: niet-bewijsbare filtering = GAT.

## Resultaat — gate correct toegepast (geen actie)

- **Hoofd-pad `/v1/adres` + `/v1/locatie`** (`_wat_geldt_hier`): via
  `p2p.mv_regel_op_locatie`, die intern `WHERE NOT r.inactief` heeft; sub-queries
  (opschrift/FTS/visie) hebben elk `AND NOT r.inactief`. ✅ *(kanttekening: MV =
  snapshot, pas na REFRESH actueel)*
- **`killer_query` / `tekst_fallback_query`** (`regelteksten_bij_vraag.py`):
  `AND NOT r.inactief` op de eind-join naar `p2p.regeling`. ✅
- **`/v1/semantisch` p2p + wro-pad** (`_SCOPE_CTE`): `AND NOT r.inactief` in de
  scope-CTE; alle kandidaten `IN (SELECT expr FROM scope)`. ✅
- **`/v1/zoek`, `/v1/normwaarde`, `/v1/activiteit`, `/v1/regeltekst`, `/v1/regels`,
  `/v1/objecten*`, `/v1/regelingen/zoek`, `/v1/viewer/regelingen`,
  `/v1/viewer/regelmix`, `/v1/viewer/objecten`**: allemaal `NOT r.inactief` /
  `r.inactief IS NOT TRUE`, direct of transitief via een gated CTE. ✅
- **Overige modules** (keywords/SKOS, expand/LLM, kennis/Chroma, leefomgeving/lev,
  vergunningen/vth, planvoorraad/wro, ponsenkaart): raken de drie doeltabellen
  niet → N.V.T. (`p2p.pons` = 33 geometrie-rijen, kolommen alleen
  `identificatie/locatie_id/was_bestemmingsplan`, geen regeling-link → geen lek).

## GATEN — te sluiten vóór de prod-prune

Gemene deler G1-G5: de frontend krijgt `expression`/`wid` normaal uit een **gated**
lijst-endpoint, dus de happy-path lekt niet. Maar geen van deze vijf **bewijst**
zelf filtering; `wid` is niet uniek (wId-fan-out); directe API-calls/bookmarks
omzeilen de lijst. Na de prune is de restblootstelling de **8 ingetrokken**
regelingen. Aanbevolen fix per site: een `EXISTS`/join-gate op
`p2p.regeling … AND NOT inactief` via `regeling_expression`, of een vroege
guard die 404/leeg geeft bij een inactieve expression.

| # | Endpoint | main.py | Aard |
|---|---|---|---|
| G1 | `/v1/viewer/tekst/{wid}` | 2481-2492 | tekst op `wid`, geen regeling-join — **hoogste risico** (directe inhoud per wid) |
| G2 | `/v1/viewer/teksten` (batch) | 2521-2531 | idem op `wid = ANY` |
| G3 | `/v1/viewer/regeling/{expression}/boom` | 2281-2376 | volledig document + annotaties op `expression` |
| G4 | `/v1/viewer/regelmix/document` (OW) | 3426-3477 | artikel-koppen op `regeling_expression`, voedt wids naar G1/G2 |
| G5 | `/v1/viewer/regeling/{expression}/ala` | 3888-3908 | ALA's + geometrie op `regeling_expression` |

**Extra (retrieval-pad, eenduidig):**

- **G6 — `semantisch.py:109-115` `_ONTWERP_SCOPE_CTE`**: mist `AND NOT r.inactief`
  (in tegenstelling tot het broertje `_SCOPE_CTE` op r. 53); ontwerp-chunks worden
  nergens hergated. Opt-in via `include_ontwerp`, inhoud is expliciet niet-vigerend
  gelabeld → lager risico, maar de fix is één regel en maakt de twee CTE's
  consistent. **Aanbevolen: nu fixen.**

## Twijfelgevallen (lager risico — beslissen)

- `/v1/onderwerp` (1360-1396): onderwerp-namen via junctie, geen gate — kan
  onderwerpen van een ingetrokken regeling als zoekterm surfacen.
- `/v1/coverage` (1286-1302): count-only; kan `has_rules=true` melden op enkel
  ingetrokken regels.
- `/v1/viewer/filter-options` (3234-3246): distinct filter-labels; een deprecated
  thema kan als optie blijven staan.

## Operationele noot (geen code-gat)

`mv_regel_op_locatie` filtert correct maar is een **snapshot** → een net-op-inactief
gezette regeling blijft zichtbaar tot REFRESH. Post-fase/prune moet de MV
verversen (doen we al in `refresh_drieslag`/post).

## Advies

1. **G6** (semantisch ontwerp) nu fixen — eenduidig, één regel.
2. **G1-G5**: sluiten vóór de prod-prune. Productvraag: mag de viewer een
   ingetrokken regeling via directe URL nog tonen (historisch inzien) of hard
   blokkeren? → bepaalt guard (404) vs. soft-flag (`inactief:true` meesturen).
3. **Twijfelgevallen**: laag; meenemen of expliciet accepteren.
