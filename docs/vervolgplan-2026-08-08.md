# Vervolgplan na de sync van 2026-08-07

*Opgesteld 2026-08-08, na de bevindingen in [sync-2026-08-07.md](sync-2026-08-07.md).
De twee kleine fixes (i2a-prefix, watermark) zijn al doorgevoerd; dit plan gaat
over de vier die meer dan een regel kosten.*

## De rode draad

De vijf gaten van deze sync hebben één gemeenschappelijke oorzaak: **de pipeline
meet of hij gedraaid heeft, niet of hij iets gedaan heeft.** Vier van de vijf
uitten zich als een nul die als succes werd gerapporteerd (i2a 334× leeg,
`n_locatie` altijd nul, GIO's nul sinds juli, `p2p.besluit` landelijk leeg). De
vijfde is de derde herhaling van hetzelfde efficiëntiepatroon (fase 4a scant
alles, net als de subdiv-storm en `naammatch_signaal`).

Dat bepaalt de volgorde hieronder: eerst het meetinstrument, dan de rest. Anders
repareren we deze vijf en vinden we de volgende vijf weer over vier maanden bij
toeval.

---

## 1. Fase-regressiecheck in het sync-rapport

**Probleem.** `SYNC-REPORT` meldt per fase "ok" of een exceptietelling. Een fase
die 342 keer een lege respons verwerkt is "ok". Er is geen vergelijking met de
vorige run, terwijl de boekhouding daarvoor al bestaat: `core.load_run`
(`n_verwerkt`, `n_fout`, per fase) en `audit.sync_run` (`totalen`, `metrics`).

**Aanpak.** Aan het eind van `full_sync.py` een vergelijking tussen deze run en
de laatste geslaagde run, met drie signalen in het rapport:

| Signaal | Betekenis |
|---|---|
| fase levert **0** waar de vorige run **> 0** gaf | harde waarschuwing — dit ving G-117 en G-119 |
| fase levert **0** en gaf ook vorige keer 0, al ≥ N runs | zachte waarschuwing — dit ving G-118 |
| aantal wijkt > X% af van het voortschrijdend gemiddelde | ter informatie |

De preview-vs-uitkomst-vergelijking die het runbook §5 al als "belangrijkste
openstaande verbetering" noemt, is hiervan een bijzonder geval: dat is dezelfde
check met de preview als verwachting in plaats van de vorige run. Bouw ze samen.

**Onzekerheid.** De drempels. Begin bewust ruw — alleen "nul waar eerder niet
nul" — en verfijn op basis van vals-positieven. Een check die iedereen wegklikt
is slechter dan geen check.

**Inschatting.** Een dag. De data ligt er, het is rapportagelogica.

**Waarom eerst.** Deze ene maatregel had drie van de vijf gaten gevonden op de
dag dat ze ontstonden.

---

## 2. `v2a_refresh.py` — de G-97-executor, met gecorrigeerde diagnose

**Probleem.** Het ontwerp uit G-97 (`v2a.embed_state` met content-hashes,
drop-by-scope, `refresh-v2a`-CLI, registratie in `core.load_run`) is voor de
helft gebouwd: de tabel bestaat en wordt gevuld, maar er is geen executor die
hem uitleest. `run_overnight.py` is nog het script van vóór dat ontwerp.

**Wat de meting van 2026-08-08 verandert.** De aanname was dat de volledige
herbouw van `chunk_annotatie` de dure post is. Gemeten:

| Fase | Gedrag | Gemeten |
|---|---|---|
| `chunk_annotatie` | volledige herbouw | 4,8 min |
| `chunk_categorie` | volledige herbouw | 4,9 min |
| objectnamen | incrementeel | 20,5 min |
| **fase 4a — embedden** | "incrementeel" | **574 van 1.979 regelingen in 139 min** |

De herbouw is dus goedkoop. De uren zitten in de **detectie**: fase 4a haalt
alle actieve regelingen op en draait per stuk de recursieve `kop_chain`-CTE over
`p2p.tekst_element` (3,1 GB), ook voor de ~1.969 waar de `NOT EXISTS`-filter
niets oplevert. Effectief 199 embeddings/min, terwijl Ollama er 25 ms over doet
(≈2.400/min).

**Aanpak.** Scope de detectie, laat de herbouw volledig:

1. Bepaal de vuile scope uit `v2a.embed_state`: vergelijk `content_hash` per
   `scope_key` met de huidige inhoud. Eén query, geen scan per regeling.
2. Draai de FETCH+embed alleen voor die scope.
3. Laat `chunk_annotatie` en `chunk_categorie` volledig herbouwen — 10 minuten
   voor een gegarandeerd consistente afgeleide is beter dan incrementele
   dirty-state met kans op scheve rijen.
4. Registreer in `core.load_run` en hang hem als `refresh-v2a` aan de CLI, zodat
   de vindlaag aan de pipeline hangt in plaats van ernaast.

**Wat er níét in moet.** `chunk_annotatie` incrementeel maken. Dat was het plan
en het levert vrijwel niets op — precies de fout die bij `naammatch_signaal` is
vermeden ("incrementeel maken bovenop een berekening die 147× te veel deed, zou
het echte probleem verstopt hebben").

**Openstaand punt dat hierbij hoort.** Chunks van verdrongen expressies worden
nooit opgeruimd; bij `gm0796` bestond 45% van de geclassificeerde wId's niet
meer in de getoonde versie. De drop-by-scope uit het ontwerp lost dat op als hij
ook op verdrongen expressies wordt losgelaten. Dat is een bewuste keuze, geen
automatisme — zie de parallel met G-91.

**Inschatting.** Twee tot drie dagen, inclusief het opruimpad.

---

## 3. GIO's (G-119) — eerst de vraag, dan de bouw

**Probleem.** `gio_zip.process_zip` wordt alleen aangeroepen vanuit losse
backfill-scripts, niet vanuit `full_sync.py` of `api_loader.py`. Regelingen
geladen in juni hebben 6.452 GIO's; juli en augustus nul.

**Eerst uitzoeken, niet bouwen.** Drie vragen, in deze volgorde:

1. **Is dit ooit anders geweest?** Als de GIO-stap nooit in de sync heeft
   gezeten, is de juni-vulling het resultaat van een eenmalige backfill en is
   "de sync laadt geen GIO's" het ontwerp, niet een regressie.
2. **Wat kost een GIO-load per regeling?** Het is een ZIP-download plus
   GML-parsing. Als dat minuten per regeling is, hoort het misschien inderdaad
   niet in de nachtelijke sync maar in een aparte, bewaakte operatie.
3. **Wat mist er functioneel zonder?** De IntIoRef → ExtIoRef → GIO-keten is de
   route van regeltekst naar geometrie-informatieobject. Meet wat er in de
   viewer/bot kapot is voor de 248 regelingen die sinds juli zijn geladen —
   niet theoretisch, maar met een concrete query.

**Daarna pas de keuze**: in de sync opnemen, of als aparte operatie houden mét
bewaking (dan valt hij vanzelf onder maatregel 1).

**Inschatting.** Een halve dag onderzoek; de bouw hangt van de uitkomst af.

---

## 4. `p2p.besluit` (G-121) — vaststellen wat het is ✅ AFGEROND 2026-08-08

**Uitkomst.** Alle drie de vragen beantwoord:

1. Geen enkele loader schrijft in deze tabellen (hele codebase gecontroleerd).
2. `GET /besluiten` op Presenteren v8 geeft **404**. De API levert het vigerende
   spoor als regelingen; het besluit als rechtshandeling zit er niet in.
3. De besluitvorming die de DSO wél levert komt uit `/ontwerpregelingen` en
   `/besluitversies` en landt in `p2pwijziging.besluit` — 445 rijen (321
   ontwerpen, 124 besluitversies).

Het is dus geen vergeten laag maar een **splitsing**: vigerende toestand uit
Presenteren, besluitvorming uit het wijzigingsspoor. De lege tabellen hebben in
`src/ddl.py` een `COMMENT ON TABLE` gekregen met deze uitleg.

**Bijvangst voor G-108**: alle 124 besluitversies hebben `nieuwe_expression` én
`begin_inwerking`; daarvan matchen er **98 op een vigerende regeling (5% van de
1.979)**. Voor die 98 is een inwerkingtredingsdatum af te leiden zonder aanname
over de FRBR-datum. De dekking groeit mee maar werkt niet terug.

*Oorspronkelijke opzet hieronder bewaard.*

### Oorspronkelijke aanpak

**Probleem.** 0 rijen in `besluit`, `besluit_regeling` en `procedurestap`, over
de hele database. De tabellen bestaan, de FK's staan, er zit niets in.

**Aanpak.** Puur onderzoek, en het is klein:

1. Schrijft enige loader ooit naar deze tabellen? (`grep` levert dat direct op —
   bij de replicatie zag ik geen INSERT-pad.)
2. Levert de Presenteren-API de besluitgegevens überhaupt? Eén call op een
   bekende regeling met een recent besluit.
3. Zo ja: is de besluitlaag nodig voor iets wat we tonen? De
   procedurestap-keten (ontwerp → vaststelling → inwerkingtreding) is
   interessant voor een tijdas, maar er hangt vandaag niets aan.

**Mogelijke uitkomst**: dit is dood schema dat beter expliciet als "niet
geïmplementeerd" gemarkeerd kan worden dan als lege tabel blijven staan — een
lege tabel suggereert dat er data hoort te zijn.

**Inschatting.** Twee uur.

---

## Volgorde en verantwoording

1. **Fase-regressiecheck** — één dag, vindt de volgende gaten vanzelf.
2. **`p2p.besluit` uitzoeken** — twee uur, kan tussendoor, sluit een open vraag.
3. **GIO-onderzoek** — halve dag, bepaalt of er iets te bouwen valt.
4. **`v2a_refresh.py`** — twee tot drie dagen, grootste tijdwinst per run.

Bewust níét in dit plan:

- **G-118 (`n_locatie`)**: de teller is kapot, maar "welke koppeling tekstdeel ↔
  regeling is dan wél juist" is een modelvraag. `divisie_wid` is een
  IMOW-annotatieverwijzing, geen wId. Repareer dat niet met een gok; het hoort
  bij de modellering van de tekstdeel-annotatie.
- **G-120 (doorwerkingsmeting, geval D)**: de reverse-top-K is een goed idee,
  maar zolang chunks van verdrongen expressies blijven staan (punt 2 hierboven)
  blijft de uitkomst scheef. Doe die volgorde niet omgekeerd.
