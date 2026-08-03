# Koepelregister — uitvoeringsplan

**Status:** vastgesteld, nog niet gestart
**Datum:** 2026-08-03
**Ontwerp:** vault `analysis/Omgevingsdocumentenregister als koepel.md`
**Besluiten:** vault `sources/Gebruikersinput.md` §[2026-08-03]

Cross-repo plan. Raakt zes repo's: een nieuwe register-repo, `OCD`, en de vier
satellieten `annotatieconformiteit.nl`, `ponsenkaart.nl`,
`instructieregels.nl`, `dso-implementatiemonitor.nl`.

---

## 1. Wat we bouwen

Een **onafhankelijk** omgevingsdocumentenregister — geen overheidsvoorziening.
Het bezit wat vandaag nergens bestaat: een vaste, deelbare URL per
omgevingsdocument en per bronhouder. De vier bestaande sites blijven
zelfstandig en leveren hun oordeel via een publieke JSON-feed.

> **Het register bezit het adres. De satellieten bezitten het oordeel.**
> Het register herberekent nooit — het toont en linkt.

Vijf schermen: zoeken · **documentdetail (nieuw)** · bronhouderprofiel ·
landelijk beeld · over het register.

---

## 2. Twee architectuurgrenzen die vooraf vastliggen

**A. Documentpagina's worden niet gebakken.** `annotatieconformiteit.nl` zit
met `out/` al tegen de Cloudflare-Pages-limiet van 20.000 bestanden aan (zie
dashboard: 18.356/20.000, later 8.933 na opschoning). Het register heeft
~25.000 documenten. Pre-renderen per document kan dus principieel niet.
Consequentie: **zoeken en documentdetail draaien live tegen `ocd-api`**;
alleen de lensstrook-cijfers en het landelijk beeld worden bij de build
opgehaald uit de vier feeds. Dat is dezelfde splitsing die
`dso-implementatiemonitor.nl` al maakt (`data-score.js` gebakken,
niets live).

**B. Nieuwe endpoints moeten naar `main`.** `OCD` heeft gedocumenteerde
branch-schuld: prod draait vanaf feature-branches via `railway up`, `main`
loopt achter. Dat is al één keer misgegaan — de `/v1/planvoorraad/*`-router
verdween van prod omdat de code alleen op `feat/rp-planvoorraad` stond en een
latere `railway up` vanaf `main` hem overschreef (ponsenkaart-dashboard,
2026-07-14). De register-endpoints lopen exact hetzelfde risico. Zie fase 0.

---

## 3. Fase 0 — precondities (blokkerend)

| # | Taak | Repo | Acceptatie |
|---|---|---|---|
| P1 | ~~Naam + domein vaststellen~~ **AF 2026-08-03** — `omgevingsdocumentenregister.nl`, domein al in bezit | — | ✅ |
| P2 | Besluiten hoe register-endpoints op `main` landen (merge-eerst, of prod-deploy expliciet vanaf `main`) | `OCD` | Elke fase levert code die op `main` staat vóór `railway up`. **Niet meer blokkerend voor livegang** — fase 1 draait op endpoints die al in productie staan (zie §4) |
| P3 | ~~Repo aanmaken~~ **AF 2026-08-03** — `RicharddeGraaf1/omgevingsdocumentenregister.nl`. Rest: Cloudflare Pages **Git-gekoppeld** (zoals de implementatiemonitor, niet direct-upload) | nieuw | Push naar `main` deployt; build output `public`; geen `.env` in de publish-directory |

P3-let-op: direct-upload heeft eerder een `CLOUDFLARE_API_TOKEN` publiek laten
lekken via `.env` op ponsenkaart. Git-koppeling met een expliciete
publish-directory vermijdt dat patroon.

---

## 4. Fase 1 — zoeken + documentdetail

Doel: een bruikbare site met één lens. Dit is de fase die het patroon bewijst.

> **Gebouwd op 2026-08-03**, en met een gunstige verrassing: er bleek **geen
> enkele OCD-wijziging nodig** om live te gaan. Een probe tegen productie gaf
> `/v1/regelingen/zoek` → 403 (bestaat, sleutel nodig) en
> `/v1/register/document/x` → 404. De vijf schermen draaien dus volledig op
> `/v1/regelingen/zoek`, `/v1/viewer/filter-options`,
> `/v1/viewer/regeling/{expr}/boom`, `/v1/viewer/wro/{idn}/detail` en
> `/v1/gezagen` — allemaal al in productie. De taken 1.1 t/m 1.5 hieronder zijn
> daarmee **verbeteringen ná livegang**, geen voorwaarden ervoor. Dat haalt de
> branchschuld (§2B) van het kritieke pad.
>
> Idem voor de lens: `annotatieconformiteit.nl/data/gezagen.json` sleutelt
> gezagen al op de kale `overheidscode` en regelingen op de AKN-`frbr_work` —
> precies de twee sleutels uit het contract. Een adapter in `public/lenzen.js`
> volstond; fase 4 hoeft voor déze satelliet niets meer te doen.

| # | Taak | Repo | Acceptatie |
|---|---|---|---|
| 1.1 | Tekst-tak van `/v1/regelingen/zoek` van `ILIKE` naar FTS. De GIN-index bestaat al: `idx_tekst_element_inhoud_fts` op `to_tsvector('dutch', coalesce(inhoud_plain,''))` (`dso-loader/src/ddl.py:231`). De `EXISTS`-subquery moet **exact dezelfde expressie** gebruiken, anders pakt Postgres de index niet | `OCD` | `EXPLAIN` toont een bitmap index scan op `idx_tekst_element_inhoud_fts`; p95 gemeten en vastgelegd |
| 1.2 | `ts_headline` toevoegen → snippet per resultaat (`{{ d.snippet }}` uit het ontwerp) | `OCD` | Snippet in de response, max ~200 tekens, treffer gemarkeerd |
| 1.3 | Titel/citeertitel/`frbr_work` blijven `ILIKE` — klein en niet-tekstueel | `OCD` | Zoeken op `NL.IMRO.…` en op `AMS_OP` werkt nog |
| 1.4 | Matview annotatietelling per regeling (act / geb / norm) | `OCD` | Kolom in zoekresultaat; refresh meeliftend op de bestaande refresh-stap |
| 1.5 | Nieuw `GET /v1/register/document/{id}` op **werk**-niveau: metadata, expressies, annotaties, geometrie-referentie. Accepteert AKN-`frbr_work` én IMRO-`idn` | `OCD` | Beide id-vormen geven 200; onbekend id geeft 404 |
| 1.6 | Frontend: zoekscherm + documentdetail, URL's `/zoeken?q=…` en `/document/<id>` | nieuw | Deelbare URL herstelt de volledige zoekstaat (het ontwerp toont die expliciet onderaan) |
| 1.7 | Lensstrook-component, één paneel, gevoed uit `annotatieconformiteit.nl/data/gezagen.json` | nieuw | Paneel toont kerncijfer, dekkingszin en doorklik; ontbrekend oordeel toont `nvt_reden` in plaats van een leeg vak |

**Uit het ontwerp schrappen:** "Inloggen bronhouder" in de header. Een
onafhankelijk register heeft geen bronhouder-accounts.

---

## 5. Fase 2 — statusdimensie

Het ontwerp draait op *in werking / vastgesteld / ontwerp / historisch*. Die
dimensie bestaat nog niet als veld.

| # | Taak | Repo | Acceptatie |
|---|---|---|---|
| 2.1 | View `p2p.v_document_status`: vigerend `p2p.regeling` → *in werking*; `p2pwijziging.besluit soort='besluitversie'` → *vastgesteld*; `soort='ontwerp'` → *ontwerp*; `regeling.inactief` → *historisch* | `OCD` | Tellingen per status sluiten aan op `p2p.regeling` (≈1.868) en `p2pwijziging.besluit` (214 = 198 + 16) |
| 2.2 | Statusfacet + statusbadge in zoeken en documentdetail | `OCD` + nieuw | Facet filtert; badge toont label **én** glyph, nooit alleen kleur |

Het ontwerp doet dat laatste al goed (`S`-map met `sLabel` + `sGlyph`) — die
keuze overnemen, niet vereenvoudigen.

---

## 6. Fase 3 — bronhouderprofiel

| # | Taak | Repo | Acceptatie |
|---|---|---|---|
| 3.1 | `GET /v1/register/bronhouder/{code}`: documentvoorraad per type + regime, OIN, laatste wijziging | `OCD` | Sluit aan op `core.bronhouder` en `/v1/gezagen` |
| 3.2 | Buurgemeenten via `ST_Touches` op `core.gemeentegrens` | `OCD` | Zwolle geeft de aangrenzende gemeenten + provincie |
| 3.3 | Frontend `/bronhouders/<code>` met tabs *Documentvoorraad* · *Transitie* · *Conformiteit*; tab *Bekendmakingen* pas na fase 7 | nieuw | Transitie-tab leest `/v1/ponsenkaart/*` + `/v1/planvoorraad/*`, herberekent niets |

---

## 7. Fase 4 — `oordeel.json` bij de overige drie satellieten

Het contract staat voluit in de vault-analyse §5. Kort: één bestand op
`https://<satelliet>/data/oordeel.json`, geïndexeerd op **twee** niveaus
(bronhoudercode en document-id), gesleuteld op `frbr_work` / IMRO-`idn` en
**niet** op de expressie, met verplichte `dekking` en `nvt_reden` als
eersterangs veld.

| Satelliet | Wat het levert | Aandachtspunt |
|---|---|---|
| `annotatieconformiteit.nl` | kerncijfer per gezag + per document | Heeft `gezagen.json` al. **Dekking is al berekend**: dekkingsgraad per regeling en categorie draait sinds 30-07-2026 — alleen exposen. Scope is `RegelingCompact`; vrijetekst (omgevingsvisie, programma), tijdelijke delen en projectbesluiten krijgen `nvt_reden` |
| `ponsenkaart.nl` | geponst % + planvoorraad-afname per gemeente | Alleen gemeenten; provincies/waterschappen krijgen `nvt_reden` |
| `instructieregels.nl` | dekking instructieregels per gemeente | Dekkingszin moet de bewijs-tier noemen — de recall is bewust laag en gestratificeerd op bewijssterkte |
| `dso-implementatiemonitor.nl` | aantal indicatoren waarop een gemeente scoort | Consumeert de andere drie al; wordt hier óók producent |

**Aanhaakpunt, geen nieuw mechanisme:** de implementatiemonitor heeft al een
lopend deelplan E *"Verversstrategie (publish-stage na OCD-load)"* — een
publish-stage die na de data-health-gate per site het data-artefact
regenereert en naar de site-repo pusht. `oordeel.json` wordt daar een extra
artefact in, niet een parallel spoor. Het openstaande punt daar (hoe pusht de
pipeline zonder interactieve login — deploy key of PAT per repo) is dus ook
hier de blokker.

---

## 8. Fase 5 — over het register + publieke API

| # | Taak | Repo | Acceptatie |
|---|---|---|---|
| 5.1 | Statische pagina: wat er in zit, juridische status, ketenplaat, begrippen, **verantwoording per lens** (wat meet hij, welke dekking, welke peildatum) | nieuw | Elke lens uit fase 4 staat er met dekking en peildatum |
| 5.2 | Onafhankelijkheidsvermelding zichtbaar in de header | nieuw | Geen ministerie-attributie, geen LVBB-ondertitel, geen toegankelijkheidsverklaring zonder echte toetsing |
| 5.3 | Besluit publieke API: nu zit alles achter `Depends(verify_key)`; het ontwerp belooft sleutelloos, CC0, 60 req/min | `OCD` | Bewuste keuze vastgelegd, met rate-limit; geen stilzwijgend opengezette sleutel |

---

## 9. Fase 6 — landelijk beeld (zonder tijdreeksen)

| # | Taak | Repo | Acceptatie |
|---|---|---|---|
| 6.1 | Provincietabel via `core.gemeentegrens.provincie` (geïndexeerd) | `OCD` | 12 provincies + expliciete regel voor Rijksdocumenten zonder provincie |
| 6.2 | Aantallen per documenttype, Ow naast Wro | `OCD` | Sluit aan op `p2p.regeling` en `wro.ruimtelijk_instrument`; historische Wro-plannen apart geteld, niet stilzwijgend meegeteld |
| 6.3 | "Opvallend in het register" — grootste omgevingsplan, meest geannoteerde activiteit, meest gebruikte gebiedsaanwijzing, hoogste genormeerde bouwhoogte | `OCD` | Elke uitschieter is doorklikbaar naar het document waar hij uit komt |
| 6.4 | Kwartaalgrafiek en "mutatie 30 dagen" **niet tonen** zolang fase 7 niet gevuld is | nieuw | Geen gereconstrueerde reeks die volledigheid suggereert |

---

## 10. Fase 7 — tijdas + bekendmakingen

| # | Taak | Repo | Acceptatie |
|---|---|---|---|
| 7.1 | `p2p`-snapshot-tabel naar het model van `wro.wro_snapshot`, forward-only | `OCD` | Eerste snapshot gezet; grafiek verschijnt pas bij ≥4 punten |
| 7.2 | Bekendmakingen (Gemeenteblad-nummers) via KOOP — aanhaakpunt `dso-loader/scripts/koop-poc/` | `OCD` | Tab *Bekendmakingen* op het bronhouderprofiel gevuld |

Zie vault [[gaps#G-105]]: de Ow-tijdas is niet volledig backfillbaar. Raakt
G-91 (intrekkings-detectie ontbreekt) — zonder die detectie is ook een
voorwaartse reeks eenzijdig.

---

## 11. Risico's

| Risico | Kans | Mitigatie |
|---|---|---|
| Register-endpoints verdwijnen van prod bij een `railway up` vanaf `main` | hoog — is al gebeurd bij `/v1/planvoorraad/*` | Fase 0 / P2: merge-eerst-afspraak, per fase gecontroleerd |
| Cijfer zonder dekking gepubliceerd naast een met naam genoemde gemeente | hoog | `dekking` verplicht in het contract; lens zonder dekkingszin wordt niet getoond. Zie [[gaps#G-104]] |
| Frontend groeit richting pre-rendered documentpagina's → Cloudflare-limiet | midden | Architectuurgrens A: documentdetail blijft live tegen de API |
| Feed-sleutel op expressie i.p.v. werk → links rotten bij elke nieuwe versie | midden | Contract sleutelt op `frbr_work` / IMRO-`idn`; expressie is een apart veld |
| FTS-omzetting verandert stilzwijgend de zoekresultaten | midden | Vóór/na-vergelijking op een vaste set queries, resultaat vastleggen |

---

## 12. Buiten scope

- Bronhouder-accounts / inloggen.
- Zelf scoren of meten: het register herberekent nooit wat een satelliet levert.
- Kaartweergave in zoeken (de lijst/kaart-toggle uit het ontwerp) — pas na fase 6.
- Wijzigen van [[model]]: dit is een ontsluitingslaag, geen domeinuitbreiding
  (vault CLAUDE.md §11.5).
