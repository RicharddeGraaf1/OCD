# Id-drift `p2p.tekst_element` tussen lokaal en prod

*Onderzocht 2026-10-30 op verzoek van de gebruiker, na de GIO-ZIP-backfill (vault G-173).
Scripts en uitvoer: `c:/tmp/id-drift-2026-10-30/` (`vergelijk_ids.py`, `impact.py`,
`tir_natuurlijk.py`, `drift.csv`). Vault: G-175.*

## Wat er aan de hand is

Lokaal en prod hebben allebei **889.729** tekstelementen, met dezelfde natuurlijke sleutels
`(regeling_expression, eid)`. Alleen bij **11.766** daarvan verschilt het id. Het is een
verwisseling binnen één aaneengesloten blok (id 1.178.840–1.190.605):

| | rijen | verschuiving (prod − lokaal) |
|---|---|---|
| 12 regelingen | 11.700 | +66 |
| gm1681 (programma) | 66 | −11.700 |

Elk verschoven id bestaat aan de andere kant ook, maar daar hoort het bij **ander tekst**.

De 13 regelingen: gm0847, gm0090, gm0202 en gm0757 (omgevingsplannen), ws0646, ws0651,
ws0665, pv26 (`2_43`), mnre1130, mnre1153, gm1681, gm1931 en gm1740.

## Herkomst

Allemaal geladen in de sync van **1 augustus 2026**. Dat was vóór de regel van 08-08 "prod
krijgt gegevens, geen loaders" (runbook §1). Toen laadde prod nog zelf, en lokaal en prod
laadden dezelfde 13 regelingen in een andere volgorde. Op prod kwam gm1681 als eerste
(`regeling_load.geladen_op` 00:16) en de rest om 02:40; lokaal kwam gm1681 als laatste.
De identity-kolom deelde de id's dus in een andere volgorde uit. De verhuizing naar Hetzner
(restore van de Railway-dump, 2026-10-28) nam de id's van prod over, en daarmee de drift.

## Wat het raakt

Elke verwijzing naar een tekstelement is aan beide kanten vertaald naar de natuurlijke
sleutel waar hij op díe database naar wijst, en die vertalingen zijn vergeleken. Scope: de 13
regelingen.

| verwijzing | lokaal | gelijk op prod | wijst op prod naar andere tekst | hoe hij naar prod gaat |
|---|---|---|---|---|
| `tekst_element.parent_id` (boom) | 11.716 | 11.716 | 0 | prod laadde zelf → consistent |
| `v2a.artikel_indeling.tekst_element_id` | 6.017 | 6.017 | 0 | `indeling_naar_productie.py` zoekt op natuurlijke sleutel |
| `p2p.tekst_inline_referentie` | 7.156 | 6.760 | **396** | ooit per id gekopieerd |
| `v2a.tekst_embedding.tekst_element_id` | 8.721 | 0 | **8.721** | `categorie-naar-productie` kopieert per id |

**De 396 verwijzingen in de tekst.** Op prod hangen ze aan een verkeerd element, en aan het
juiste element ontbreken ze (396 = 396). Verdeling: pv26 161, ws0651 129, ws0665 51,
mnre1130 39, en 4 elk bij gm0847, gm0090, gm1931 en gm1681. Naar soort: IntRef 155,
ExtIoRef 86, IntIoRef 79, ExtRef 76. Bij ~400 is het element-id op prod precies het lokale
getal, dus ze zijn per id gekopieerd. Waarschijnlijk is dat gebeurd bij het
GIO-koppelingsherstel (G-119) met `repliceer_p2p_naar_prod.py --expressies`, dat
`tekst_inline_referentie` op id upsert. Dat is niet getoetst.
**Gevolg voor gebruikers:** de API leest deze tabel om bij een artikel de GIO's en de kaart
te vinden (`ocd-api/main.py`, de IntIoRef/ExtIoRef-keten). Voor die artikelen kan de viewer
een GIO bij het verkeerde artikel tonen, of hem bij het juiste missen.

**De 8.721 chunks.** Alle chunks van de 13 regelingen wijzen op prod naar ander tekst.
**Vandaag zonder zichtbaar effect.** `ocd-api/semantisch.py` gebruikt de eigen tekst, kop,
`wid` en regeling van de chunk, en geen enkele API-query of de instructieregels-query springt
van chunk naar tekstelement. Het is wel een valkuil voor elke toekomstige query die dat doet.

Ook de id's van `tekst_inline_referentie` zelf zijn verschoven (dezelfde oorzaak). Er
verwijst niets naar, dus dat is onschadelijk.

## Waarom de controles het niet zagen

`diff_lokaal_prod.py` telt rijen per tabel, en die zijn gelijk. `repliceer_p2p_naar_prod.py`
slaat een rij over die op prod onder een ander id staat ("bestaan al onder
(regeling_expression, eid) met een ander id"). Pas de backfill van 30-10, die deze
regelingen opnieuw aanbood, liet dat zien: 10.689 overgeslagen, en daarna een unique-violation
op `activiteit_locatieaanduiding` (`uq_ala_natural`). Die tabel heeft dezelfde soort drift.
Die is hier niet uitgezocht.

## Herstel — oorspronkelijk voorstel (uitgevoerd, zie hieronder)

Prod gelijktrekken met lokaal voor de 13 regelingen. Lokaal is de waarheid en de id-set is
identiek:

1. Op prod per regeling de tekstelementen verwijderen (cascade naar
   `tekst_inline_referentie`) en de lokale rijen invoegen mét hun id
   (`OVERRIDING SYSTEM VALUE`). De id's zijn vrij zodra de 13 regelingen weg zijn, want de
   set is aan beide kanten gelijk. Eén transactie.
2. `artikel_indeling` opnieuw via `indeling_naar_productie.py` (natuurlijke sleutel), en de
   `element_hertaling`-MV verversen. De chunks kloppen daarna vanzelf.
3. Dezelfde toets voor `activiteit_locatieaanduiding` (en andere tabellen met een identity-pk
   die per id naar prod gaan) vóór je die aanraakt.

Afdwingen in plaats van onthouden: `diff_lokaal_prod.py` zou per regeling een
vingerafdruk over `(id, eid)` van `tekst_element` kunnen vergelijken, en niet alleen een
telling. Dan valt id-drift bij de eerstvolgende sync op.

## Herstel, uitgevoerd 2026-10-30

Script: `dso-loader/scripts/herstel_id_drift_prod.py` (droogloop-default, één transactie per
stap, natellen vóór de COMMIT). Logs in `c:/tmp/id-drift-2026-10-30/`.

**Eerst de impact op alle andere tabellen in kaart.** Wat aan een tekstelement-id hangt:

| | koppeling | bij het herstel |
|---|---|---|
| `tekst_element.parent_id` | FK, cascade | gaat mee (lokale boom, lokale id's) |
| `tekst_inline_referentie` | FK, cascade | gewist en lokaal terug |
| `v2a.artikel_indeling` | FK, **cascade** | wordt door het wissen meegewist → lokaal terug (inhoud was al gelijk) |
| `v2a.tekst_embedding` | id zonder FK | klopt daarna vanzelf (droeg al de lokale id's) |
| MV's met `te.id`: drieslag, `mv_element_hash`, `mv_bronhouder_health`, **`mv_regel_op_locatie`** | rekenen met het id | op prod opnieuw berekend |
| views (`element_hertaling`, `v_vrijetekst_health`, …) | live | niets |
| `irm.*` | regel-identificatie en chunk-id's | niets |

**1. Tekstelementen (13 regelingen).** Droogloop vóór: 11.766 tekstelementen, 792 afwijkende
verwijzingssleutels (396 weg + 396 verkeerd), 8.721 chunks, boom en indeling 0. Echt: 11.766
weg (met cascade), 11.766 met het lokale id terug, plus 7.156 verwijzingen en 6.017
indelingsrijen. Vóór de COMMIT was er op alle vijf onderdelen 0 afwijking. Dat kostte 17 s.
Daarna `mv_element_hash` (38 s). Landelijke nameting: **0 van de 889.729 tekstelementen met
een ander id**.

**2. De andere tabellen met een eigen id**, vergeleken op inhoud per id (hele tabel):

| tabel | rijen | zelfde inhoud, ander id | actie |
|---|---|---|---|
| `tekst_inline_referentie` | 624.921 | 0 (na stap 1) | — |
| `activiteit_locatieaanduiding` | 408.723 | **67** | `--rijtabel`: 67 weg, 67 terug met lokaal id |
| `normwaarde` | 28.262 | **11** | idem: 11 |
| `kaartlaag` | 1.241 | 0 | — |

Naar die rijen verwijst niets. Het herstel is daar dus alleen nodig voor replicatie per id: bij
ALA botste die (`uq_ala_natural`), bij normwaarde zou hij stil een andere rij overschrijven,
want die tabel heeft geen natuurlijke unieke sleutel. De andere tabellen met een eigen id
(vindlaag, i2a, wijzigingsspoor) worden in hun geheel mét id's gespiegeld en zijn gelijk.
MER laadt op prod zelf, en tussen de databases gaat daar niets per id.

**3. Bijvangst: `p2p.mv_regel_op_locatie` werd nooit ververst.** Deze MV draagt `te_id`, en de
API joint erop (`ocd-api/main.py`, de regel-op-locatie-route van bot en viewer). Hij stond in
geen enkele sync-stap. Prod liep dus op de stand van de restore van 28-10 (392.743 rijen over
1.425 regelingen), lokaal nog ouder (378.788 over 1.403). Nieuwe regelingen ontbraken en
ingetrokken regelingen bleven zichtbaar. Na het id-herstel wees hij bovendien naar andere
tekst. Ververst op beide kanten: **394.156 rijen over 1.421 regelingen**, 9 s op prod. Hij
staat nu als stap 1e in `refresh_drieslag.py`, dus elke sync neemt hem mee.

**Nog open:** de vingerafdruk op `(id, eid)` in `diff_lokaal_prod.py`, zodat id-drift bij de
volgende sync vanzelf opvalt in plaats van bij toeval.
