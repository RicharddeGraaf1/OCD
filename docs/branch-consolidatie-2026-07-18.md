# Branch-consolidatie — main ↔ productie (2026-07-18)

Analyse van de branch-schuld: **`main` loopt fors achter op wat er in productie
draait**. Prod is herhaaldelijk gedeployed via `railway up` vanaf feature-branches,
zonder die branches naar `main` te mergen. Deze notitie inventariseert het gat en
adviseert een veilige consolidatie.

## Landschap (2026-07-18)

| Repo | Branch | Commits vóór main | Rol |
|---|---|---|---|
| OCD | `feat/vector-chunk-lagen` | 44 (superset) | grab-bag; bevat `feat/retrieval-kernel-convergentie` (34) + vector-index-werk |
| OCD | `feat/retrieval-kernel-convergentie` | 34 | retrieval-kernel + wro-imro2006 + BOPA + annotatie-fixes |
| OCD | `feat/rp-planvoorraad` | 1 | planvoorraad |
| OCD | `feat/vectorindex-lagen` | 0 | schone vector-cherry-picks — al op main |
| OCDviewer | `feat/intent-fastpath-viewer` | 7 | data-actualiteit + intent-fastpath (incl. commit van vandaag) |

**Belangrijk**: de commit-telling overdrijft. Veel is al op main via cherry-picks
(bevestigd: de inactief-mechaniek, de schone vector-index met
`include_wro`/`include_ontwerp`, en vandaag run_log/BOPA-loader/MER/load-status).
De netto **inhoudelijke** diff `main ↔ feat/vector-chunk-lagen` is ~8.000 regels
over 43 bestanden — dát is de echte maat.

## Wat er GENUINE op prod draait maar op main ONTBREEKT

Per-feature geverifieerd (bestand/functie afwezig op main):

| Feature | Prod-status (uit dashboard) | Op main? |
|---|---|---|
| `wro_imro2006.py` — IMRO2006-plannen (ambtsgebied-geometrie, 3.736 plannen) | live 2026-07-05 | **MIST** |
| `wro_planvoorraad.py` — RP-planvoorraad-snapshot | live | **MIST** |
| `ocd-api/intent.py` + `rank.py` + `fastpaths.py` — retrieval-kernel (bot↔viewer-convergentie) | live | **MIST** |
| `vergunningen`-`/v1/vergunningen/doorlooptijd` — voor omgevingsvergunningenregister.nl | live 2026-06-29 | **MIST** |
| `2026-07-classify-afwijkvergunning.py` — BOPA-register-classificatie | live 2026-07-02 | **MIST** |
| `api_loader` regeltekst_wid-fix (449eebd) — annotatie volgt nieuwe versie | correctheidsfix | **MIST** |
| vth-geometrie G-87 (multi-marker-selectie) | live | te verifiëren |
| BOPA DSO-satelliet-loader (`load-ovg`) | live | ✅ vandaag hersteld |
| data-health/load-status + run_log | — | ✅ vandaag hersteld |
| schone vector-index + `include_wro/ontwerp` | live | ✅ (cherry-pick eerder) |

Dit zijn **≥5 productie-features + een live API-endpoint** die alleen op branches
staan. Dat is reële schuld met correctheidsimpact (de regeltekst_wid-fix).

## Waarom niet blind mergen

1. `feat/vector-chunk-lagen` is bewust een **grab-bag** (dashboard: "grab-bag …
   bewust niet gemerged; schone branch feat/vectorindex-lagen cherry-picked").
   Wholesale mergen haalt ook experimenteel/afgedankt werk binnen.
2. De working tree heeft **ongerelateerde WIP** (gio_zip.py, refresh_drieslag.py,
   losse ?? scripts) die niet mee moet.
3. Prod is niet SHA-verifieerbaar vanuit deze omgeving (geen Railway-toegang), dus
   "main = prod" kan niet blind worden aangenomen.

## Aanbevolen consolidatie (veilig, feature-voor-feature)

1. **Verifieer de prod-SHA** in Railway (welke commit draait ocd-api-production nu?).
   Dat is het ankerpunt voor "waarheid".
2. **Cherry-pick de genuine-missing features** naar main in reviewbare brokken,
   in deze volgorde (laag risico → hoog):
   - ✅ correctheidsfix `api_loader` regeltekst_wid (cherry-pick a0b1169) +
     data-remediatie (6316cee: `herlaad_annotaties` + CLI `herlaad-annotaties-stale`,
     194 stale expressies her-annoteerd, gm0556/gm0880 → 0). **KLAAR 2026-07-18.**
   - ✅ `wro_imro2006.py` + `wro_planvoorraad.py` + DDL/CLI/SQL (code-consolidatie;
     data stond al in dev). **KLAAR 2026-07-18.**
   - ✅ `vergunningen` doorlooptijd-endpoint (`/v1/vergunningen/doorlooptijd`,
     per_type bopa/regulier) + `classify-afwijkvergunning.py` + afwijk-kolommen in
     KOOP_DDL + dossier_doorlooptijd-matview-SQL. Endpoint e2e 200. **KLAAR 2026-07-18.**
   - ⏳ retrieval-kernel (`intent.py`/`rank.py`/`fastpaths.py` + main.py-integratie) —
     **grootst + riskantst, NOG TE DOEN**. Anders dan de andere vier: geen schone
     file-add maar ~511 regels divergentie in `main.py` (serveert álle endpoints) +
     een gedrags­wijziging in retrieval. Vereist de golden-set-eval
     (`build_viewer_golden_set.py` + `kernel_retrieval_eval.py` + `viewer_golden_set.json`)
     als regressie-vangnet vóór commit. Aanrader: aparte, gefocuste sessie —
     eerst main.py-integratie in kaart, dan porten, dan eval-vergelijking main-vs-kernel.

**Residu-hiaat** (uit de remediatie): ~242 regels over 189 expressies houden een
stale regeltekst_wid ná her-annotatie — een ándere oorzaak (regel verwijst naar een
wId-type dat niet als tekst_element wordt opgeslagen), niet de versie-staleness.
Geen drilldown-breuk; aparte data-quality-vraag.
3. **Deprecate de grab-bag**: zodra de genuine features op main staan, tag
   `feat/vector-chunk-lagen` als archief en verwijder 'm, zodat hij niet opnieuw
   als "bron van waarheid" wordt aangezien.
4. **OCDviewer**: `feat/intent-fastpath-viewer` (7 commits) apart naar main brengen
   of expliciet als de nieuwe main adopteren.

## Root cause + procesfix

**Oorzaak**: deployen via `railway up` vanaf een branch koppelt "live" los van
"gemerged". De branch is de waarheid, main niet.

**Fix (afspraak)**: merge naar main **vóór of direct ná** een deploy; deploy bij
voorkeur vanaf main (of via GitHub Actions op push naar main). Nooit een feature
alleen op een branch laten staan nadat 'ie live is. Zie ook de terugkerende
"stond op ongemergde branch"-vondsten in de vault-log (data-health, BOPA).
