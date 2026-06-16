# Prompt — Audit van `core.bronhouder` (code ↔ naam-uitlijning)

> Plak dit als opdracht in een nieuwe sessie met toegang tot de OCD-repo.

---

## Taak
Voer een systematische audit uit van `core.bronhouder` in de OCD-database en
spoor alle **code ↔ naam-misalignments** op (codes met een verkeerde of
ontbrekende naam). Lever een rapport + kant-en-klare correcties.

## Context
- DB: Postgres in Docker-container `dso-postgis` (lokaal). Verbind via
  `from src.db import get_conn` vanuit `c:/GIT/OCD/dso-loader` (leest `.env`).
- `core.bronhouder` (kolommen o.a. `overheidscode`, `naam`, `bestuurslaag`).
- **Oorzaak van de bug**: de `naam` wordt bij eerste-insert handmatig via de CLI
  meegegeven (`load-ow --overheid 'pvXX,Naam'`) en door `ON CONFLICT DO NOTHING`
  daarna nooit gecorrigeerd. Typefouten blijven dus plakken.
- **Al gevonden + gefixt op 2026-06-07** (ter illustratie van het patroon):
  pv21 stond op "Groningen" → Fryslân · pv25 "Flevoland" → Gelderland ·
  pv30 "Gelderland" → Noord-Brabant · mnre1130 code-only → Ministerie van IenW ·
  ws0653 code-only → Wetterskip Fryslân (de échte WV-publisher; let op: ws0680
  heet óók "Wetterskip Fryslan" maar publiceert geen WV — verdacht duplicaat).
  Deze hoeven niet opnieuw; de audit moet vaststellen of er **méér** zijn.

## Methode — leid de waarheid af, vertrouw de bestaande naam niet
Per bronhouder de echte identiteit afleiden uit autoritatieve bronnen en
vergelijken met `bronhouder.naam`:
1. **Regeling-opschrift** (sterkste bron): `p2p.regeling.opschrift` /
   `citeertitel` bevat vaak de echte naam ("Omgevingsverordening Fryslân 2022",
   "Waterschapsverordening Wetterskip Fryslân").
2. **Standaard-codes**:
   - Provincies (CBS): pv20=Groningen, pv21=Fryslân, pv22=Drenthe,
     pv23=Overijssel, pv24=Flevoland, pv25=Gelderland, pv26=Utrecht,
     pv27=Noord-Holland, pv28=Zuid-Holland, pv29=Zeeland, pv30=Noord-Brabant,
     pv31=Limburg.
   - Gemeenten (`gmXXXX` = CBS-gemeentecode): kruis-check tegen PDOK; de loader
     `src/loaders/gemeentegrens_pdok.py` haalt autoritatieve gemeentenamen op.
   - Waterschappen/rijk: afleiden uit opschrift (geen vrije vaste standaard).
3. **Code-only labels** (`naam == overheidscode`, bv. "ws0653", "mnre1018"):
   altijd verdacht → oplossen uit opschrift.
4. **Duplicaat-namen** binnen één bestuurslaag (twee codes, zelfde naam): mis.

## Te onderzoeken (minimaal)
- Alle **12 provincies** — tegen de CBS-standaard én opschrift.
- Alle **35 waterschappen** — let op ws0680/ws0653-achtige duplicaten/foutlabels;
  match opschrift "Waterschapsverordening <naam>".
- **6 rijk-codes** — label de code-only mnre's uit hun regeling-opschriften.
- **Gemeenten (474)** — steekproef + gerichte duplicaat- en code-only-check.

## Deliverable
1. Tabel: `overheidscode | huidige naam | afgeleide juiste naam | bewijs
   (opschrift/standaard) | zekerheid (hoog/midden/laag)`.
2. Kant-en-klare `UPDATE core.bronhouder SET naam=… WHERE overheidscode=…;` voor
   de hoog-zekere gevallen — **eerst tonen, pas uitvoeren na akkoord**.
3. Aanbeveling voor de **structurele fix**: een canonieke code→naam-map (minstens
   provincies + rijk) in `ow_loader.load_ow_overheid` / `cli.py load-ow`, met
   `ON CONFLICT … DO UPDATE SET naam=…` voor die gecontroleerde lagen, zodat
   handmatige CLI-typefouten niet meer blijven plakken.

## Let op
- **Read-only** tijdens de analyse; geen writes zonder expliciet akkoord.
- Raak `overheidscode` (PK, FK-doelwit) nóóit aan — alleen `naam`.
- Een afwijkende naam kán correct zijn (een overheid kan onder meerdere codes
  bestaan); leun op het opschrift-bewijs, niet op aannames.
