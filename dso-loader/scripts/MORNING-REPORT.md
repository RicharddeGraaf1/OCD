# Overnight-run vector-chunk-lagen
Gestart. Fase 3+4+5 over het volle corpus.

## Fase 3 — canonieke v2a.chunk-laag
Kolommen wid/source_type/source_id/source_ref + backfill + view `v2a.chunk`. Dicht G-88.

## Fase 4a+5 — embed volledig corpus
Van 1704355 naar **1715018 chunks** (+10663). 15 regelingen aangevuld. Idempotent/resumable per tekst_element.

## chunk_annotatie herbouwd
816850 rijen over 556786 chunks; 550873/1715018 = 32% met werkingsgebied.

## chunk_categorie uitgebreid
787519 toewijzingen (nearest bevestigde centroide) over alle Lid/Divisietekst-chunks. Taxonomie ongewijzigd (v1).

## Fase 4b — object-namen
+617 objectnaam-chunks (activiteit/gebiedsaanwijzing/norm), gescoped per (object, regeling) via annotatie. Voor de activiteit-as-retrieval; geen werkingsgebied-links (tekst_element_id NULL).

## Corpus-eindstand
- **1715635 chunks** over **41882 regelingen**
- per bron_soort: Artikel=765092, Lid=446058, Divisietekst=341461, Begrip=103498, objectnaam=41969, Overig=13884, Bijlage=1627, Hoofdstuk=1567, Regels=311, Toelichting=86, Paragraaf=82

## Git
Branch `feat/vector-chunk-lagen` (was `main`), commit van 6 bestanden. **Niet gepusht** — doe jij 's ochtends.


---
_Totale looptijd: 179 min. Zie overnight.log voor details._