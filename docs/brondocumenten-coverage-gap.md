# Brondocumenten-coverage gap — 12 cases uit omgevingsbot V6.19 L1-eval

**Datum**: 2026-05-18 (geverifieerd tegen DB-stand)
**Status**: Groep A vrijwel opgelost — 3 van de 5 oorspronkelijk-ontbrekende bronnen blijken inmiddels wél in OCD te staan (gevolg van de URL-encoding-fix en de Wro-structuurvisie-loader uit eerdere sessies). De resterende 2 cases (R37/R38 Ontwerp Nota Ruimte) worden bewust geaccepteerd als out-of-scope. Groep B (geo-koppeling) staat nog open.
**Companion**: zie [data_gap_research_prompt.md](./data_gap_research_prompt.md) voor het diagnose-protocol; deze doc is de actuele case-lijst per 2026-05-18.

## Achtergrond

De omgevingsbot V6.19 L1-eval (`run_pipeline_eval.py`) is verrijkt met checks
op `prompt.full_input` — de échte string die naar de LLM gaat. Voor 12 van de
32 positieve testcases (40 - 8 negatieve) faalt nu de check
`prompt.full_input contains '<bron_naam>'`. Dat is geen lint-mismatch, het
betekent: de bot bouwt zijn context op zonder de regeling die volgens de
test-set autoritair zou moeten zijn voor die locatie.

Voor elke van de 12 is gecheckt of de regeling überhaupt in OCD's
`p2p.regeling` staat. De uitkomst splitst de 12 in twee scherpe groepen.

## Groep A — DATA-GAP (5 cases, 3 unieke bronnen)

Status per 2026-05-18, geverifieerd door directe queries op `p2p.regeling` en `wro.ruimtelijk_instrument`.

| Case | Bevoegd gezag | Bron | Status | Vindplaats |
|---|---|---|---|---|
| R26 | Ministerie LVVN | Aanwijzingsbesluit Natura 2000-gebied **Alde Feanen** | ✅ in OCD | `p2p.regeling` → `/akn/nl/act/mnre1153/2025/AldeFeanen` |
| R27 | Ministerie LVVN | Aanwijzingsbesluit Natura 2000-gebied **Alde Feanen** | ✅ in OCD | idem (zelfde regeling) |
| R37 | Ministerie VRO | **Ontwerp Nota Ruimte** | ❌ niet in OCD (geaccepteerd) | DSO `/ontwerpregelingen`, `/akn/nl/act/mnre1171/2025/000003` |
| R38 | Ministerie VRO, Gem. Neder-Betuwe | **Ontwerp Nota Ruimte** | ❌ niet in OCD (geaccepteerd) | idem |
| R39 | Provincie Gelderland (pv25) | **Omgevingsvisie Gaaf Gelderland** | ✅ in OCD | `wro.ruimtelijk_instrument` → `NL.IMRO.9925.SVOmgvisieGG-vst1` (structuurvisie, vastgesteld 2018-12-19) |

### R26/R27 — Alde Feanen: hoe het alsnog binnenkwam

Het Alde Feanen-besluit kwam binnen via de **URL-encoding bug-fix** in `dso-loader/src/loaders/api_loader.py::_encode_regeling_uri`. DSO Presenteren v8 vervangt zowel `/` als `-` door `_` in regelingen-paden — de oude loader deed alleen `/`-substitutie en kreeg daardoor 404 voor regelingen met date- of UUID-segmenten (waaronder een set N2000-aanwijzingsbesluiten). Na de fix is een herlaad-run (`scripts/herstel_url_bug.py`, 235 regelingen) Alde Feanen ingevoerd.

Conclusie: er was geen aparte LVVN-selectie-bug; het was een transport-bug die *een willekeurige subset* van mnre1153-besluiten raakte.

### R39 — Gaaf Gelderland: in Wro, niet in p2p

`Omgevingsvisie Gaaf Gelderland` is feitelijk een Wro-structuurvisie (regime=RO in de testset, IMRO-codering `NL.IMRO.9925.SVOmgvisieGG-vst1`). Niet in `p2p.regeling`, maar wel in `wro.ruimtelijk_instrument` als `type_plan=structuurvisie`. Binnengekomen via de uitbreiding van `dso-loader/src/loaders/wro_pdok.py` met `_load_structuurvisieplangebied` (115 provinciale structuurvisies geladen).

Voor de bot betekent dit dat de adres-pipeline ook `wro.ruimtelijk_instrument` moet raadplegen voor Gelderland-vragen die naar een SV verwijzen.

### R37/R38 — Ontwerp Nota Ruimte: bewust niet in scope

Ontwerp Nota Ruimte staat in DSO als ontwerpregeling onder `mnre1171` (ministerie VRO), URI `/akn/nl/act/mnre1171/2025/000003`, type Omgevingsvisie. De huidige `p2pwijziging`-loader pakt alleen besluiten/wijzigingen waarvoor een vigerende voorganger bestaat — ontwerpen-zonder-voorganger vallen buiten scope. mnre1171 staat ook niet in de loader-config.

Deze cases worden **geaccepteerd als out-of-scope**: we breiden de loader hier niet voor uit.

## Groep B — GEO-KOPPELING-ISSUE (7 cases, 5 unieke bronnen)

De regeling staat WEL in OCD, maar `/v1/adres` / `/v1/locatie` geeft hem niet terug voor deze locatie. Onderzoek nodig: heeft de regeling locaties? Liggen die op de query-coords?

| Case | Bevoegd gezag | Bron | DB-row | Type |
|---|---|---|---|---|
| R20 | Provincie Overijssel | Omgevingsverordening Overijssel | pv23, Omgevingsverordening | OV |
| R21 | Wetterskip Fryslân | Waterschapsverordening Wetterskip Fryslân | ws0653, Waterschapsverordening | WV |
| R22 | Wetterskip Fryslân | Waterschapsverordening Wetterskip Fryslân | ws0653, Waterschapsverordening | WV |
| R34 | Ministerie IenW | Programma Integraal Riviermanagement | mnre1130, Programma | PR |
| R35 | Provincie Flevoland | Natuurbeheerplan Flevoland | pv24, Programma | PR |
| R36 | Provincie Flevoland | Natuurbeheerplan Flevoland | pv24, Programma | PR |
| R40 | Provincie Gelderland | Projectbesluit Windpark Echteld - Lienden | pv25, Projectbesluit | PB |

**Vermoedelijke oorzaken** (te verifiëren per case via Check 2 in [data_gap_research_prompt.md](./data_gap_research_prompt.md)):
- **Programma's / Visies / Projectbesluiten** (R34, R35, R36, R40) hebben vaak géén `activiteit_locatieaanduiding`-records — ze hebben locatie-gerelateerde tekst maar geen IMOW-objectkoppeling waar `/v1/adres` op join't. De huidige API-flow ondersteunt ze niet.
- **Waterschapsverordening** (R21, R22) — Wetterskip Fryslân heeft locatie-bereik over het Waddengebied; query-coords liggen wellicht buiten de geometrie of de geometrie is niet correct ingest.
- **Omgevingsverordening Overijssel** (R20) — verordening voor bodemenergie-systemen heeft mogelijk landelijk-bereik (heel Overijssel) maar dat is in IMOW vaak per ambtsgebied geannoteerd; query-coord-intersect kan stuk zijn.

## Per-case detail

### R20 — warmtepomp diep in bodem Overijssel
- Locatie: `Aronskelkstraat 38, 7531TS Enschede` (adres)
- Vraag: *Hoe diep in de bodem mag ik hier een warmtepomp aanleggen?*
- Verwacht antwoord: artikel 3.42, aanleggen bodemenergiesystemen dieper dan 5 meter verboden
- Status: bron WEL in DB (pv23)

### R21 — grondwater onttrekken Wetterskip
- Locatie: `Perceel Oldeboorn (ODB04) F 405`
- Vraag: *Welke regels gelden hier voor het onttrekken van grondwater voor besproeien van gewassen?*
- Verwacht antwoord: afdeling 3.2 Onttrekken van grondwater, artikel 3.8 t/m 3.16
- Status: bron WEL in DB (ws0653), maar `/v1/gezagen` toont ws0656 (niet ws0653) als wetterskip — kan zijn dat ws0653 een aparte loader gebruikt
- Tweede issue: detector `_ACTIVITEIT_QUESTION_PATTERN` matcht niet op deze vraagvorm

### R22 — water aan meertje onttrekken
- Locatie: `172304, 607360` (rd-coord, Fryslân)
- Vraag: *Mag ik hier water aan het meertje onttrekken?*
- Verwacht antwoord: artikel 3.5, vergunningplicht, Waddengebied
- Status: bron WEL in DB (ws0653); pipeline pakt activiteit-A maar uit *Omgevingsverordening Fryslân 2022* i.p.v. Waterschapsverordening

### R26 — aalscholver Alde Feanen
- Locatie: `De Sayter 1, 9003XT Warten` (adres in Fryslân, in N2000-gebied Alde Feanen)
- Vraag: *Is de aalscholver hier een beschermde diersoort?*
- Verwacht antwoord: ja, artikel 2 lid 3
- Status: ✅ bron WEL in DB (`p2p.regeling`, `mnre1153/2025/AldeFeanen`). Binnengekomen via de URL-encoding-fix.

### R27 — aalscholver Garyp (buiten Alde Feanen)
- Locatie: `Inialoane 23, 9263RB Garyp` (adres net buiten N2000)
- Vraag: *Is de aalscholver hier een beschermde diersoort?*
- Verwacht antwoord: niet bepaald, buiten N2000 — `expected_no_answer=false` maar het verwachte antwoord is "geen aanwijzing"
- Status: ✅ bron WEL in DB (zelfde regeling als R26). De geo-check kan nu daadwerkelijk negatief uitvallen voor Garyp.

### R34 — hoogwaterbescherming IenW Tolkamer
- Locatie: `Rijnstraat 1-13, 6916BC Tolkamer` (adres)
- Vraag: *Wat is hier het beleid t.a.v. hoogwaterbescherming?*
- Verwacht antwoord: meerdere teksten, o.a. par 2.3
- Status: bron WEL in DB (mnre1130 = IenW als Programma); maar Programma's hebben typisch geen IMOW-locatie-koppeling die `/v1/adres` ophaalt

### R35 / R36 — subsidie natuurbeheer Flevoland (perceel resp. RD-coord)
- Locatie R35: `Perceel Lelystad (LLS00) R 214`
- Locatie R36: `170239, 489741` (RD)
- Vraag: *Welke regels gelden hier over aanvragen van subsidie voor natuur- en landschapsbeheer?*
- Verwacht antwoord: H.4 subsidiemogelijkheden
- Status: bron WEL in DB (pv24 als Programma); zelfde geo-koppeling-issue als R34

### R37 / R38 — Ontwerp Nota Ruimte VRO
- Locatie R37: `Inialoane 23, 9263RB Garyp` (adres)
- Locatie R38: `Saneringsweg 3a, IJzendoorn` (adres)
- Status: ❌ bron NIET in DB en wordt **niet alsnog geladen**. Bestaat in DSO als ontwerpregeling onder `mnre1171` (VRO), URI `/akn/nl/act/mnre1171/2025/000003`, type Omgevingsvisie. De `p2pwijziging`-loader pakt alleen wijzigingen op een vigerende voorganger — ontwerpen-zonder-voorganger zijn out-of-scope.

### R39 — Omgevingsvisie Gaaf Gelderland
- Locatie: `Laan van Westenenk 701, 7334DP Apeldoorn` (adres)
- Vraag: *Welke ambitie is hier t.a.v. het terugdringen van broeikasgassen?*
- Verwacht antwoord: H4 par 1, ambitie 2050 Gelderland klimaatneutraal, tussendoel 2030 55% reductie
- Status: ✅ bron WEL in DB, maar in `wro.ruimtelijk_instrument` (`NL.IMRO.9925.SVOmgvisieGG-vst1`, structuurvisie, vastgesteld 2018-12-19). Binnengekomen via `wro_pdok._load_structuurvisieplangebied`. De adres-pipeline moet hiervoor `wro.ruimtelijk_instrument` raadplegen, niet `p2p.regeling`.

### R40 — zienswijzen Windpark Echteld
- Locatie: `Saneringsweg 3a, IJzendoorn` (adres, in Gelderland)
- Vraag: *Kunnen er nog zienswijzen worden ingediend op het plan voor de aanleg van een windpark hier?*
- Verwacht antwoord: einde inzagetermijn van ontwerpbesluit was 19-02-2025
- Status: bron WEL in DB (pv25 als Projectbesluit, plus aparte "Omgevingsplanregels vanwege projectbesluit"); Projectbesluit-type komt niet uit `/v1/adres` zoals het nu joins

## Werk-prompt

> **Voor de OCD data-engineer / agent**:
>
> Lees deze doc + [data_gap_research_prompt.md](./data_gap_research_prompt.md) (het algemene diagnose-protocol). Golf 1 is grotendeels afgerond — alleen Golf 2 (geo-koppeling) staat nog open.
>
> ### Golf 1 — Data-gap (Groep A) — STATUS PER 2026-05-18
>
> - ✅ **R26/R27 Alde Feanen**: opgelost via URL-encoding-fix in `api_loader._encode_regeling_uri` + herlaad-run. Geen verdere actie.
> - ✅ **R39 Gaaf Gelderland**: opgelost via uitbreiding `wro_pdok._load_structuurvisieplangebied`. Adres-pipeline moet `wro.ruimtelijk_instrument` raadplegen voor SV-content.
> - ❌ **R37/R38 Ontwerp Nota Ruimte**: bewust niet opgelost. Vereist een aparte ontwerp-loader voor ontwerpen-zonder-vigerende-voorganger en een toevoeging van `mnre1171` (VRO) aan de bronhouder-config. Out-of-scope tot de eval-set deze cases anders modelleert.
>
> ### Golf 2 — Geo-koppeling fixen (Groep B, 7 cases / 5 bronnen)
>
> Voor elke bron in groep B het diagnose-protocol uit [data_gap_research_prompt.md](./data_gap_research_prompt.md) §Check 2 doorlopen:
>
> 1. Haal RD-coord van de testcase op (`/v1/adres?q=<location>` of `/v1/locatie?x=...&y=...`).
> 2. Query `p2p.locatie` om te zien of de regeling locaties heeft die deze coord intersecten.
> 3. Drie sub-verdicts:
>    - **No locations at all** → regeling heeft géén IMOW-locatie-records (typisch voor Programma's en Projectbesluiten met enkel ambtsgebied-bereik). Vereist `/v1/adres`-uitbreiding om ambtsgebied-fallback toe te voegen voor PR/PB/SV-documenttypes.
>    - **Locations exist but don't intersect** → geo-fout. Check ST_IsValid + SRID.
>    - **Locations intersect maar regel zit er niet bij** → join-pad in `/v1/adres` mist iets (waarschijnlijk een niet-actieve join voor Programma/Projectbesluit).
>
> Specifieke verwachte bevindingen:
> - **R34, R35, R36, R40** (Programma + Projectbesluit) — vrijwel zeker subverdict "no locations at all". Fix: in `/v1/adres` een join op `regeling.ambtsgebied` ⊃ query-coord toevoegen voor `documenttype IN ('Programma','Projectbesluit','Omgevingsvisie','Structuurvisie')`.
> - **R20** (Omgevingsverordening Overijssel) — bouwhoogte-regel artikel 3.42 heeft ambtsgebied-bereik (heel Overijssel). Zelfde fix als hierboven maar voor `Omgevingsverordening` waar `activiteit_locatieaanduiding` ontbreekt.
> - **R21, R22** (Waterschapsverordening Wetterskip) — heeft wel WV-content; check `ws0653` vs `ws0656` als bronhouder-code (`/v1/gezagen` toont ws0656 als wetterskip met `ow_geladen=false`, maar `p2p.regeling` toont ws0653 — naam-mismatch op bronhouder-niveau?).
>
> ### Acceptatiecriterium per case
>
> Een case is gefixt zodra de omgevingsbot V6.19 L1-eval (
> `python -m tests.evaluation.run_pipeline_eval --case r{NN}` in
> `C:\GIT\omgevingsbot.nl\backend`) status PASS geeft op de check
> `prompt.full_input contains '<bron-substring>'`. Verwacht effect na
> Golf 1+2 over de 10 in-scope cases (R37/R38 uitgezonderd): ~+10 PASS
> in L1-eval. Voor R26/R27/R39 ligt de bal nu bij de adres-pipeline:
> de bron staat in OCD, alleen de geo-koppeling/tabelkeuze moet kloppen.
