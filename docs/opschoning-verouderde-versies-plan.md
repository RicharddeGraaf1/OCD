# Plan — opschonen verouderde regelingversies & ontwerpen

*Opgesteld 2026-07-24. Status: plan, nog niet uitgevoerd.*

Doel: teksten die **niet meer in de nieuwste versie** van een regeling staan
(verdrongen expressies) en **verouderde ontwerpen** zijn niet meer vindbaar in de
OCD-database — noch via retrieval (verbergen), noch als fysieke bloat (prune).

Kennisbank-tegenhanger: vault `analysis/Opschonen verouderde versies en ontwerpen
uit de OCD-database.md` + gap G-95.

---

## Bevindingen (grondslag)

- **Marking bestaat**: `p2p.regeling.inactief` + `reden_inactief`, gezet bij
  (re)load via `src/versie_status.py::markeer_siblings_inactief` en achteraf via
  `scripts/markeer_verouderde_expressies.py`.
- **Retrieval filtert** `AND NOT r.inactief` op de kern-joins → verdrongen versies
  al grotendeels verborgen.
- **Meting 2026-07-24**: 185/2151 regelingen inactief (177 `verouderde-versie`,
  8 `ingetrokken`).
- **Cascades** (gemeten): `p2p.regeling → p2p.tekst_element` = ON DELETE CASCADE;
  `tekst_element →` `tekst_inline_referentie`, `p2pwijziging.tekst_element`,
  `conv.tekst_element` = CASCADE. Dus een DELETE van inactieve regeling-rijen
  ruimt de tekst-keten op.
- **⚠️ Kritisch gat**: `v2a.tekst_embedding` heeft **géén FK** naar
  `p2p.tekst_element` (losse `tekst_element_id`). Een prune laat dus **verweesde
  embeddings** achter (blijven in de HNSW-index). Die moeten **expliciet** weg.

---

## Deelproblemen & concrete stappen (aanbevolen volgorde)

### A. Verbergen 100% dichttimmeren  *(eerst — laag risico, direct effect)*

1. **Inventariseer** alle retrieval/zoek-ingangen die inactieve data kunnen
   surfacen: bot-kernel, viewer `killer_query`/`tekst_fallback`,
   `/v1/semantisch`, `/v1/adres`, directe SQL in `ocd-api/*.py`. Check per pad of
   `NOT inactief` (of de scope-CTE) wordt toegepast.
2. **Centraliseer** de gate in één `p2p.v_regeling_vigerend`-view of een gedeelde
   `_SCOPE_CTE`, zodat een nieuwe query 'm niet kan vergeten.
3. **NULL-expressie-annotaties** (G-86 §1): ~160 `juridische_regel`-rijen met
   `regeling_expression = NULL` (wId-fan-out-fallback) zijn niet via
   `regeling.inactief` te filteren. Optie: backfill de expressie via `wid` →
   `tekst_element`, of markeer expliciet.
4. **Embeddings-scope**: bevestig dat élke vector-query de inactief-gate draagt
   (join op `regeling_expression`/`wid` → `regeling.inactief`).

**Klaar als**: geen enkel productie-pad levert nog een inactieve expressie op
(toetsbaar met een gerichte testset).

### B. Verdwenen uit het DSO markeren (G-91)  *(tweede — laag risico)*

- `scripts/diff_dso_bronhouder_coverage.py` vult al `core.bronhouder_dso_diff`
  (`n_over` = lokaal overbodig). Neem dit **in de sync-keten** op (nieuwe stap in
  `fase_post` of losse post-stap), verifieer verdwenen works via de Presenteren-API,
  en zet dan `inactief=true, reden_inactief='ingetrokken'`. **Nooit blind
  verwijderen** — markeren is herstelbaar.

### C. Verouderde ontwerpen/besluiten opruimen (G-86 §6)  *(derde)*

- 74 van 392 `p2pwijziging.besluit` wijzen naar een inmiddels **vigerende**
  expressie (gerealiseerd, maar blijft "aankomend" in `/v1/wijzigingen`).
- Cleanup-stap symmetrisch met `markeer_verouderde_expressies.py`:
  `load_alle_ontwerpen` een opschoon-fase geven die besluiten waarvan
  `nieuwe_expression` inmiddels vigerend is, markeert of verwijdert.

### D. Fysieke prune van verdrongen versies (G-86 §7)  *(laatst — hoog risico)*

Los `scripts/prune_verouderde_versies.py`, **resumable**, dry-run-default,
**nooit tijdens een load**:

```sql
-- 0. bepaal de te prunen set (alleen verouderde-versie, niet ingetrokken/
--    NULL-reden; alleen wat aantoonbaar verdrongen is)
CREATE TEMP TABLE te_prunen AS
  SELECT frbr_expression FROM p2p.regeling
  WHERE inactief AND reden_inactief = 'verouderde-versie';

-- 1. embeddings EXPLICIET (geen FK-cascade!) — batch-gewijs
DELETE FROM v2a.tekst_embedding
  WHERE regeling_expression IN (SELECT frbr_expression FROM te_prunen);

-- 2. annotatie-tabellen: verifiëren of ze cascaderen vanaf regeling of
--    tekst_element; zo niet, expliciet meenemen (ALA, normwaarde, locatie,
--    juridische_regel, activiteit_locatieaanduiding, …)

-- 3. regeling-rijen (cascade ruimt tekst_element + tekst_inline_referentie +
--    p2pwijziging.tekst_element + conv.tekst_element)
DELETE FROM p2p.regeling
  WHERE frbr_expression IN (SELECT frbr_expression FROM te_prunen);

-- 4. onderhoud: ANALYZE + eventueel REINDEX van de HNSW/GIN-indices
```

Veiligheids-checklist:
- dry-run/count vóór elke DELETE-stap (toon volumes per tabel);
- batches (bv. per bronhouder of per 50 expressies) i.v.m. lock-/WAL-druk;
- draai buiten load-vensters; log elke stap in `core.load_run`
  (`bron='prune-verouderd'`) zodat het zichtbaar is in het dashboard;
- het retrieval-filter (A) blijft als vangnet staan — prune is voor de bloat,
  niet voor de correctheid.

---

## Ontwerpprincipes

- **Hide-first, dan prune.** A + B + C maken alles correct-verborgen (laag risico);
  D haalt daarna de fysieke bloat weg.
- **Markeren vs verwijderen.** Intrekkingen/verdwenen (B) → markeren (herstelbaar).
  Verdrongen versies (D) → DELETE mag, want de vigerende versie is de bron van
  waarheid.
- **Filter + prune hybride.** Het `NOT inactief`-filter blijft ook ná de prune —
  defence-in-depth.
- **Nooit prunen tijdens een load** (geen leeg venster voor lezers).

## Nog te meten / bevestigen

- Exacte volumes (tekst_element + embeddings) op de 185 inactieve expressies.
- Cascade-gedrag van de annotatie-tabellen (regeling vs tekst_element).
- Volledige lijst retrieval-ingangen die `NOT inactief` (nog) missen.
