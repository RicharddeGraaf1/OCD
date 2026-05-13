# IMTR-activiteit-koppeling — data-gap analyse

**Datum**: 2026-05-12
**Status**: data-gap geconstateerd; loader-fix nodig
**Companion**: zie `C:\GIT\omgevingsbot.nl\docs\20260510_uniform-zoektermen-contract.md`
voor de bot-kant. De bot heeft deze koppeling nodig om voor "anders geduid"-
activiteit-vragen de toepasbare regel (DMN-vraagboom) als focused context te
kunnen aanbieden i.p.v. een vrije regeltekst-blob.

## Waarom dit bestaat

Een omgevingsbot-gebruiker stelt vragen als "Mag ik hier een dakopbouw bouwen?".
De pipeline matcht dat op een IMOW-activiteit in `p2p.activiteit`. Voor de
meeste activiteiten staat in `p2p.activiteit_locatieaanduiding.kwalificatie`
een scherpe juridische status: `verboden | vergunningplicht | meldingsplicht |
toegestaan`. Daar kan de bot deterministisch antwoorden op formuleren.

Voor cases waar de kwalificatie `anders geduid` is (of ontbreekt), bestaat
er meestal wél een **toepasbare regel** — een DMN-vraagboom die het bevoegd
gezag heeft gepubliceerd om vergunningplicht uit te rekenen per scenario.
Dat is de juiste bron voor zulke vragen.

OCD slaat de DMN-XML al op (`i2a.dmn_element` heeft 930k rijen), maar **de
koppeling tussen DMN-content en IMOW-activiteit is nu niet gemaakt**. De bot
kan vanuit een activiteit-ID dus niet bij de bijbehorende vraagboom komen.

## Huidige stand in `i2a`

| Tabel | Rijen | Status koppeling naar `p2p.activiteit` |
|---|---|---|
| `werkzaamheid` | 291 | `activiteit_id`: 148/291 gevuld (51%) |
| `regelbeheerobject` | 57.459 | geen directe koppeling naar IMOW-activiteit |
| `toepasbaar_regelbestand` | 53.379 | indirect via `regelbeheerobject` (fsr) |
| `uitvoeringsregel` | 388.887 | `activiteit_urn`: **0/388.887 gevuld** ⚠️ |
| `dmn_element` | 930.123 | geen directe; alleen via `regelbestand_ns` |

De kolom `i2a.uitvoeringsregel.activiteit_urn` is in het schema gedefinieerd
en zou de directe brug naar `p2p.activiteit.identificatie` vormen, maar is
voor geen enkele uitvoeringsregel gevuld. Dat is de eerste, en belangrijkste,
data-gap.

Daarnaast is `i2a.werkzaamheid.activiteit_id` voor 143 van 291 werkzaamheden
nog leeg. Werkzaamheden zijn er voor 'mag ik X?'-niveau-vragen (bv.
`DakkapelPlaatsen` → `nl.imow-gm0569.activiteit.Dakkapel`). Een hogere
coverage hier helpt secundair: ze geven de bot een "vergunningcheck-naam"
om naar te verwijzen, ook zonder de DMN-content.

## Wat ik in OCD geverifieerd heb

Bij een query op een typische `dakopbouw`-activiteit
(`nl.imow-gm0344.activiteit.4ad736bbfdb74548be08b53f9e5a8bb9`):

```sql
SELECT regel_type, COUNT(*) AS n
FROM i2a.uitvoeringsregel
WHERE activiteit_urn = '<id>'
GROUP BY regel_type;
-- 0 rijen
```

Dezelfde activiteit heeft wel een werkzaamheid-match, maar:

```sql
SELECT urn, naam, activiteit_id FROM i2a.werkzaamheid
WHERE naam ILIKE '%dakopbouw%' OR naam ILIKE '%dakkapel%';
-- 1 rij: 'Dakkapel plaatsen, vervangen of veranderen'
--        urn=DakkapelPlaatsen, act=nl.imow-gm0569.activiteit.Dakkapel
```

Eén voorbeeld dus, en niet voor de Utrecht/Amersfoort-activiteiten waarvoor
de bot een vraag binnenkrijgt.

## Wat de bot ermee zou doen (als koppeling er was)

Pseudocode voor het `anders geduid`-pad in de activiteit-fast-path:

```python
if act_kwalificatie == "anders geduid":
    toepasbare_regel = ocd.query_toepasbare_regel(activiteit_id=act_id)
    if toepasbare_regel.dmn_elements:
        # Path C — DMN-vraagboom als focused context voor LLM
        focused_context = build_dmn_focused_context(toepasbare_regel)
    elif toepasbare_regel.werkzaamheid_naam:
        # Path D — hint dat er een vergunningcheck bestaat
        focused_context = f"Er bestaat een vergunningcheck '{...}' bij gemeente X."
    else:
        # Geen IMTR — terug naar regular flow
        pass
```

Dat vereist een nieuw endpoint `/v1/toepasbare-regel?activiteit_id=X` aan
OCD-zijde, dat joint over:

```
p2p.activiteit
  → i2a.uitvoeringsregel (via activiteit_urn) ← deze koppeling moet gevuld
    → i2a.toepasbaar_regelbestand (via regelbestand_ns)
      → i2a.dmn_element (via regelbestand_ns)
```

Plus een fallback via werkzaamheid:

```
p2p.activiteit
  → i2a.werkzaamheid (via activiteit_id) ← coverage van 148/291 mag omhoog
```

## Voorgestelde fixes

### Fix 1 (kritisch) — `uitvoeringsregel.activiteit_urn` invullen

Locatie: `C:\GIT\OCD\dso-loader\src\loaders\imtr_loader.py`, functie
`_parse_and_store_dmn` (lijnen 190-243).

Tijdens het parsen van DMN-XML wordt nu wel `regelbestand_ns` als brug naar
de uitvoeringsregel opgeslagen, maar niet de activiteit-URN waarop de regel
betrekking heeft. De activiteit-koppeling zit in de STTR-XML in tags zoals
`<uitv:activiteit>` of `<uitv:bereik>` (afhankelijk van schemaversie).

Te onderzoeken:
1. STTR-XML-schema-versie identificeren (header van een sample).
2. Welk element bevat de activiteit-URN per uitvoeringsregel? Mogelijk
   `uitv:bereik` met een href, of `uitv:doelregistratie`, of een sibling-
   `<activiteit>`-tag binnen `<uitvoeringsregel>`.
3. In de loader-code `uitv.find("uitv:bereik", DMN_NS).text` (of equivalent)
   ophalen en als `activiteit_urn` meegeven aan de INSERT.

Eén sample STTR-bestand handmatig openen geeft de XPath snel. De huidige
`_parse_and_store_dmn` slaat de activiteit-link gewoon over.

### Fix 2 (secundair) — `werkzaamheid.activiteit_id` coverage omhoog

Locatie: idem `imtr_loader.py`, functie `_load_werkzaamheden` (lijnen 246-308).

De huidige logica doet:

```python
for k in koppelingen:
    act_urn = k.get("urn", "")
    if act_urn:
        UPDATE werkzaamheid SET activiteit_id = %s
        WHERE urn = %s
          AND EXISTS (SELECT 1 FROM p2p.activiteit WHERE identificatie = %s)
        ...
        if cur.rowcount > 0:
            break  # ← stopt bij eerste hit
```

Het `break` na de eerste werkende koppeling betekent: als de DSO-API
meerdere `activiteitKoppelingen` retourneert voor een werkzaamheid, en de
eerste matcht niet met een rij in `p2p.activiteit`, dan worden de
volgende niet geprobeerd. Resultaat: 143 werkzaamheden waar geen koppeling
gemaakt is. Mogelijke oorzaken:
- de werkzaamheid wijst naar landelijke activiteit-URN's die niet als
  bronhouder-activiteit zijn ingegest;
- de gekoppelde activiteit zit in een gemeente waarvoor de OW-loader niet
  is gedraaid;
- de URN heeft een net andere vorm dan `p2p.activiteit.identificatie`.

Onderzoek:
1. Voor de 143 niet-gekoppelde werkzaamheden de API-response loggen — welke
   activiteit-URN's worden aangedragen?
2. Per URN nakijken of die in `p2p.activiteit` voorkomt, en zo nee, waarom.
3. Als ze in een ander schema zitten (bv. `core.activiteit_waardelijst` voor
   landelijke activiteiten), de koppeling daarheen leggen.

### Fix 3 (optioneel) — nieuw endpoint `/v1/toepasbare-regel`

Pas zinvol nadat fix 1 is gedraaid. Signature:

```
GET /v1/toepasbare-regel?activiteit_id=<imow-urn>
  → 200 {
      "activiteit_id": "...",
      "uitvoeringsregels": [
          {"regel_type": "Vraag", "naam": "...", "dmn_id": "...", "parent_id": "..."},
          ...
      ],
      "regelbestanden": [
          {"namespace": "...", "naam": "...", "geldig_begindatum": "..."}
      ],
      "werkzaamheid": {"urn": "DakkapelPlaatsen", "naam": "Dakkapel plaatsen..."}
  }
```

SQL-template:

```sql
SELECT u.regel_type, u.regelbestand_ns, de.dmn_id, de.naam, de.parent_id,
       tr.naam AS regelbestand_naam, tr.geldig_begindatum
FROM   i2a.uitvoeringsregel u
LEFT JOIN i2a.dmn_element de ON de.regelbestand_ns = u.regelbestand_ns
                            AND de.id = u.dmn_element_id
LEFT JOIN i2a.toepasbaar_regelbestand tr ON tr.namespace = u.regelbestand_ns
WHERE  u.activiteit_urn = :activiteit_id
ORDER BY de.parent_id NULLS FIRST, de.id;
```

Plus werkzaamheid-lookup als fallback:

```sql
SELECT urn, naam FROM i2a.werkzaamheid WHERE activiteit_id = :activiteit_id;
```

## Wat dit oplost

De omgevingsbot heeft drie nu zwakke case-clusters die hiermee zouden
herstellen:

1. **R15, R18, R19** (Utrecht "Mag ik hier een [dak-X] bouwen?"). De
   IMOW-kwalificatie is `anders geduid`. Path B met vrije regeltekst geeft
   nu LLM-noise. Met DMN-vraagboom kan de bot exact zeggen welke stappen de
   gebruiker moet doorlopen om vergunningplicht te bepalen.
2. **R21** (Wetterskip Fryslân grondwater-onttrekking). Detector mist;
   keyword-bucket vindt 15 rijksregels-activiteiten die de echte bron
   (Waterschapsverordening Wetterskip 2023) missen. Een werkzaamheid-koppeling
   zou het juiste signaal geven.
3. **R34** (Programma Hoogwaterbescherming). Programma-cases vallen nu
   buiten elke fast-path; een werkzaamheid-koppeling op
   programma-activiteiten zou helpen.

Daarnaast levert het structurele waarde voor het
`Annotatieconformiteit`-traject: zichtbaar maken welke bronhouders welke
toepasbare regels publiceren per IMOW-activiteit, en waar de
publicatie-volledigheid gaten heeft.

## Niet in scope

- Het parsen van de DMN-vraagboom tot een vraag/antwoord-flow. De bot
  ontvangt de boom als structured data en laat de LLM 'm samenvatten voor
  de gebruiker.
- IMTR-data voor RTR-bestuursorganen waarvoor OCD geen OW-content heeft
  ingegest. Geen werk om de gat te dichten in dit doc — eerst bot-side
  meten of het nuttig is.

## Werk-schatting

| Stap | Tijd |
|---|---|
| Fix 1 — STTR-XML-schema duiken + loader patch | 4-6 uur |
| Re-load IMTR voor PoC-gemeente om koppeling te vullen | 30 min |
| Fix 2 — werkzaamheid-koppeling-loop fixen | 1-2 uur |
| Fix 3 — `/v1/toepasbare-regel` endpoint + tests | 2-3 uur |
| **Totaal** | **~1 dag** |
