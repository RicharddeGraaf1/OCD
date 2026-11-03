# Draaiboek: echte plangrenzen voor Wro-plannen (G-177)

Opgesteld 2026-11-03. Branch `wro-plangrens` in `c:/GIT/ocd-kenmerken`, gecommit, nog niet gepusht.
Stappen 1 t/m 4 zijn klaar. **Stap 5 en 6 alleen na akkoord van Richard.**

## Waarom

3.736 plannen hadden de hele gemeente als plangrens en verschenen daardoor op elk adres in die
gemeente. De IHR levert de echte grens wel (`GET /plannen/{id}?expand=geometrie`).

## Stand op 2026-11-03, 23:15

| | Plannen |
|---|---|
| Bijgewerkt, alleen lokaal (`geometrie_herkomst='ihr-plangrens'`) | 3.736 |
| Nog te doen (`geometrie_herkomst='ambtsgebied-imro2006'`) | 0 |
| Productie | niets gewijzigd |

Uitkomst nachtrun 22:00-23:02: 2.705 bijgewerkt, 0 zonder grens, 0 niet in de IHR, 0 fouten.
Mediaan 0,052 km², 0 ongeldig, 0 leeg. 21 plannen houden minstens 90% van de oude grens; dat zijn
gemeentebrede plannen als TAM-facetplannen, veegplannen en buitengebied.

| Punt | Wro-documenten vóór | Na de run |
|---|---|---|
| Dam 1 | 7 | 7 |
| Does 30, Utrecht | 12 | 12 |
| Oldeboorn | 4 | 4 |
| Broekhem 33 | 1 | 1 |
| Oude IJsselstreek | 25 | 3 |
| Hof van Twente | 34 | 13 |
| Rijssen-Holten | 30 | 13 |

Steekproef van 25 willekeurige plannen tegen de IHR: 25/25 genoemd op een punt binnen de grens,
25/25 niet genoemd op een punt net buiten de grens.

Lijst van alle 3.736 plannen vóór de wijziging:
`C:\Users\RdG2\AppData\Local\Temp\claude\c--GIT-OmgevingswetKnowledgeBase-vault-v1\25d17023-fce0-4571-84b5-09c95355236b\scratchpad\plangrens_nulmeting_2026-11-03.csv`

## Stap 1: de run (vanavond na 22:00)

In deze sessie staat een achtergrondtaak klaar die om 22:00 start en logt naar
`c:/tmp/wro-plangrens-nacht.log`. Is de sessie weg, start dan handmatig in Git Bash, tussen 22:00
en 06:00:

```bash
cd c:/GIT/ocd-kenmerken/dso-loader
env $(grep -E '^(DB_|IHR_API_KEY|DSO_)' c:/GIT/OCD/dso-loader/.env | xargs) PYTHONUTF8=1 \
  python -c "from src.cli import cli; cli()" load-wro-plangrenzen >> c:/tmp/wro-plangrens-nacht.log 2>&1
```

- `PROD_DB_URL` gaat bewust niet mee: dit raakt alleen de lokale database (`localhost:5434`).
- Duur: ongeveer 45 minuten, 1 aanroep per seconde.
- De loader weigert buiten het venster, stopt als het sluit en stopt na 3 fouten op rij.
- Afgebroken? Hetzelfde commando pakt alleen de rest op. Na 3 fouten op rij niet doorduwen:
  uitzoeken wat er is en de volgende nacht opnieuw.

## Stap 2: uitkomst controleren

```bash
tail -5 c:/tmp/wro-plangrens-nacht.log
```

De laatste regel toont `bijgewerkt`, `geen_grens`, `niet_in_ihr` en `fout`. Daarna, op de lokale database:

```sql
SELECT coalesce(geometrie_herkomst, '(pdok)') AS herkomst, count(*),
       percentile_cont(0.5) WITHIN GROUP (ORDER BY ST_Area(geometrie) / 1e6) AS mediaan_km2,
       count(*) FILTER (WHERE NOT ST_IsValid(geometrie)) AS ongeldig,
       count(*) FILTER (WHERE ST_IsEmpty(geometrie)) AS leeg
FROM wro.ruimtelijk_instrument GROUP BY 1;
```

Goed is:

- `ihr-plangrens` plus `ambtsgebied-imro2006` samen 3.736, met `ambtsgebied-imro2006` alleen nog
  voor plannen zonder grens of niet in de IHR;
- `ongeldig` en `leeg` zijn 0 bij `ihr-plangrens`;
- de mediaan van `ihr-plangrens` ligt ver onder 1 km².

## Stap 3: nameten

API uit de werkkopie starten tegen de lokale database, op poort 8013:

```bash
cd c:/GIT/ocd-kenmerken/ocd-api
env DATABASE_URL="postgresql://$(grep ^DB_USER= c:/GIT/OCD/dso-loader/.env | cut -d= -f2):$(grep ^DB_PASSWORD= c:/GIT/OCD/dso-loader/.env | cut -d= -f2)@localhost:5434/$(grep ^DB_NAME= c:/GIT/OCD/dso-loader/.env | cut -d= -f2)" \
  python -m uvicorn main:app --port 8013
```

Per punt `http://127.0.0.1:8013/v1/viewer/regelmix?x=..&y=..`, documenten met `bron_type='wro'`.
Gebruik `127.0.0.1`: `localhost` kost op Windows 2 seconden per aanroep.

| Punt | x, y | Wro-documenten vóór | Verwacht na de run |
|---|---|---|---|
| Dam 1 (al bijgewerkt) | 121394, 487383 | 7 | 7 |
| Does 30, Utrecht (al bijgewerkt) | 128983, 455222 | 12 | 12 |
| Oldeboorn (al bijgewerkt) | 188661, 560278 | 4 | 4 |
| Broekhem 33 (al bijgewerkt) | 185904, 320095 | 1 | 1 |
| Oude IJsselstreek | 224113, 435436 | 25 | minder |
| Hof van Twente | 235503, 472740 | 34 | minder |
| Rijssen-Holten | 226860, 477893 | 30 | minder |

Een plan dat op een van de laatste drie punten blijft staan, moet er ook echt liggen. Toets een
paar steekproeven tegen de IHR (`POST /plannen/_zoek`, dat zijn enkele aanroepen).

## Stap 4: tests en commit op de branch

```bash
cd c:/GIT/ocd-kenmerken/dso-loader && python -m pytest tests/test_wro_plangrens.py -q
```

Daarna `test_viewer.py` in `ocd-api` tegen de lokale database; die stond op 93 groen. Commit op
`wro-plangrens`, **niet pushen**. Werk `gaps.md` (G-177) en `log.md` in de vault bij, en geef
Richard de uitkomst.

## Stap 5: code naar `main` (alleen na akkoord)

Een push naar OCD `main` is een productie-deploy. Inhoudelijk verandert de API niet: de
wijzigingen zitten in de loader. Eerst `git fetch` en controleren of `main` is verschoven.

## Stap 6: data naar productie (alleen na akkoord)

Runbook stap 6f, met de tunnel van `dso-loader/scripts/prod_tunnel.sh`. Let op: die tunnel laat
na het stoppen `ssh.exe` op poort 15433 draaien; expliciet opruimen en natellen.

```bash
cd c:/GIT/OCD/dso-loader
python scripts/wro_planobject_naar_productie.py        # droogloop, geen vlaggen
```

- De vergelijking neemt geometrie en herkomst mee, dus de bijgewerkte plannen staan als `gewijzigd`.
- **Controleer de droogloop.** `gewijzigd` hoort ongeveer het aantal `ihr-plangrens`-plannen te
  zijn. Staat er veel meer (lokale ladingen die nog niet op productie staan), of plannen onder
  `verdwenen`, stop dan en overleg.
- Pas daarna `--ja`. Geen `--verwijder-verdwenen`.
- De planobjecten veranderen niet (deze plannen hebben er 0), dus de tegellaag hoeft niet opnieuw.
- Controle op productie: Dam 1 via `https://omgevingsdocumentenregister.nl/api/v1/viewer/regelmix?x=121394&y=487383`
  moet 7 Wro-documenten geven (nu 14). Daarna de browsertests.

## Terugdraaien

Lokaal: voor elk plan met `geometrie_herkomst='ihr-plangrens'` de geometrie terugzetten op
`core.gemeentegrens` (of `core.gemeentegrens_historisch`) van de `bronhouder`, en de herkomst op
`ambtsgebied-imro2006`. De CSV hierboven bevat ter controle de oorspronkelijke oppervlakte per plan.

## Open, buiten dit draaiboek

- Deze plannen hebben geen planobjecten. Binnen het plan kan de site dus niet filteren op
  bestemmingsvlak. De plannen zijn klein (mediaan 0,052 km²), dus het weegt niet zwaar.
- Ontwerpversies staan als `actief`, en een enkele vastgestelde versie uit de IHR ontbreekt. Op
  Rijssen-Holten staan *Parapluplan parkeernormen* en *Parapluplan risicobronnen* in de IHR, maar
  bij ons in geen enkele versie. Zie G-177.
