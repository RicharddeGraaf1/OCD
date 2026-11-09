# Plan — hertaal-achterstand wegwerken met Sonnet

*Opgesteld 2026-11-06 door Claude op verzoek van Richard. Startwoord in een sessie:
**"loop verder met het plan"** (of "volgende hertaal-portie"). Claude leest dan dit
bestand in `c:/GIT/OCD-vergunningen/docs/` (main, de bijgewerkte versie), neemt de
eerste open portie uit de tabel onder §4 en werkt §3 af. Scripts en data draaien in
`c:/GIT/OCD/dso-loader` (daar staat `.env`).*

## 1. Waarom en wat

De sync hertaalt alleen wat die nacht geladen is (runbook stap 6bis). Alles wat
daarvóór geladen is en nooit een Sonnet-hertaling kreeg, blijft liggen. Gemeten
2026-11-06 in de lokale DB:

| | unieke teksten |
|---|---|
| Lid en Divisietekst in actieve regelingen zonder Sonnet/v1-hertaling | **66.616** |
| waarvan hooguit 3.000 tekens: **dit plan** | **57.765** (46,3 mln tekens, gem. 801) |
| waarvan langer dan 3.000 tekens: geparkeerd, zie §5 | 8.851 |

Bijna alles komt maar één keer voor (singletons), dus de dedup helpt hier
nauwelijks: één hertaling per element.

**Waarom Sonnet en niet Haiku**: twee proeven op 150 open teksten, 2026-11-06
(vault-log, twee entries "Proef hertaling"). Haiku 5.5 is inhoudelijk minstens
even goed, maar blijft 0,8 punt achter op leesbaarheid (3,6 tegen 4,4 op 5), ook met
een aangescherpte prompt. Leesbaarheid is het doel van de hertaling.

**Prompt**: ongewijzigd v1, zoals de sync hem gebruikt (`OPDRACHT` in
`hertaal_fanout_export.py`). Modellabel in de cache: `claude-sonnet-5`, net als in
de sync. Geen tweede soort hertaling onder dezelfde `prompt_versie`.

## 2. Indeling

- **Portie = één nacht.** Portie 1 is een meetnacht van **3.000** teksten. Daarna
  **6.000** per portie; de laatste neemt de rest. Dat is 10 porties.
- **Batch = één Sonnet-agent, 120 teksten** (~100 kB `.txt`, net als de sync van
  04-11 met 86 teksten op 97 kB). Een portie van 6.000 is dus 50 agents.
- **Tien agents tegelijk**; start een nieuwe zodra er een klaar is. Gemeten in
  portie 1: 3 tot 4,5 minuut per agent, 3.000 teksten in 12 minuten. De tijd is dus
  geen beperking, de gebruikslimiet wel.
- **Volgorde**: eerst teksten uit omgevingsplannen (gm), dan provincies (pv),
  waterschappen (ws) en de rest. Binnen een groep op hash, dus kort en lang door elkaar.
- **Elke portie haalt zelf op wat nog open is.** De export sluit uit wat al in de
  cache staat. Een half afgemaakte portie is dus geen probleem: laad wat er is, en de
  rest komt vanzelf in de volgende portie mee.

Na de meetnacht: kijk naar het gebruik op het abonnement en stel de portiegrootte
bij in de tabel onder §4. Is 6.000 te veel, neem dan 4.000. Kost hij weinig, neem
dan 9.000.

## 3. Draaiboek per portie

Werkmap per portie: `c:/GIT/OCD/dso-loader/data/hertaal-achterstand/portie-NN`
(`data/` staat in `.gitignore`). Logs in dezelfde map.

**0. Vooraf**
- Niet in dezelfde nacht als een sync. Draait er een, sla de portie over.
- Lokale DB leeft: `wsl -l -v` (een open poort 5434 bewijst niets).
- Portie N-1 is geladen (§4). Is hij dat niet, laad hem dan eerst (stap 3).

**1. Export** (~20 s, alleen lezen)
```bash
cd c:/GIT/OCD/dso-loader
python scripts/hertaal_fanout_export.py --alles --max-lengte 3000 \
    --max <grootte uit §4> --per-batch 120 --dir data/hertaal-achterstand/portie-NN \
    > data/hertaal-achterstand/portie-NN.export.log 2>&1
```
Controleer: aantal teksten = de grootte uit §4, of minder bij de laatste portie.
Het script schrijft per batch `.json`, `.txt` en een lege `tmp-NN/`.

**2. Agents** — tien tegelijk, `model: sonnet`, `subagent_type: general-purpose`,
golf na golf tot alle batches klaar zijn. Prompt per agent (NN invullen):

```
Je maakt begrijpelijke varianten van juridische teksten uit de Nederlandse
omgevingsregelgeving. Dit is batch NN van een hertaalronde.

Werkmap: c:/GIT/OCD/dso-loader/data/hertaal-achterstand/portie-PP
- Lees de opdracht: OPDRACHT.md in die map. Volg de regels voor de hertaling daaruit
  woordelijk (systeemrol, 3-4 korte zinnen, geen jargon, inhoudelijk trouw, geen markdown).
- Lees de invoer uit batch-NN.txt (NIET de .json: die heeft regels van duizenden tekens
  die afgekapt gelezen worden). Elk item heeft `SLEUTEL:` (dit is de bron_hash) en
  `TEKST:`. Lees het hele bestand, ook in delen als het groot is.
- Eigen map voor tussenbestanden: tmp-NN/. Schrijf nergens anders tijdelijke bestanden.
- Uitvoer: out-NN.jsonl in de werkmap. Eén JSON-object per regel, exact:
  {"bron_hash": "<ongewijzigd>", "tekst": "<hertaling>"}. Eén regel per item.

Kritisch:
- Kopieer de sleutel teken voor teken. Verzin geen sleutels.
- Schrijf elke hertaling zelf, specifiek voor die tekst. Genereer de hertalingen niet
  met een script of sjabloon. Een klein script om je geschreven regels als geldige
  JSONL weg te schrijven mag wel.
- Doe het werk zelf; start geen subagents.
- Werk in porties van ongeveer 40 teksten en schrijf na elke portie weg.
- Controleer aan het eind met een korte telling dat het aantal regels gelijk is aan
  het aantal items en dat elke regel geldige JSON is.

Rapporteer als eindantwoord alleen het aantal verwerkte teksten en het pad, niet de
hertalingen zelf.
```

**Gebruikslimiet bereikt** (agents falen met een limietmelding of schrijven niets):
start geen nieuwe golf meer. Laat lopende agents afmaken, ga naar stap 3 en laad
met `--deels`. Noteer in §4 hoeveel er af is. De rest komt in de volgende portie.

**3. Natellen en laden**
```bash
python scripts/hertaal_fanout_laad.py --dir data/hertaal-achterstand/portie-NN          # droogloop
python scripts/hertaal_fanout_laad.py --dir data/hertaal-achterstand/portie-NN --ja     # laden
```
- Gat in de sleutelset: draai één agent op `batch-herstel.json` (uitvoer
  `out-herstel.jsonl`) en herhaal. Bij een afgebroken nacht: `--ja --deels`.
- **Sjabloontoets** voor het laden: per `out-NN.jsonl` het aantal unieke teksten en
  de meest voorkomende eerste drie woorden. Bijna alles uniek is goed. Herhaalt een
  batch zich (meer dan 5 dezelfde teksten of één vaste opening in de meeste regels),
  gooi die batch dan weg en draai hem opnieuw.
- **Steekproef**: lees 10 willekeurige paren (bron en hertaling) uit verschillende
  batches. Klopt er één inhoudelijk niet, noteer dat in §4.

**4. Naar prod** (~3 min, schrijft naar productie; dit plan is de toestemming)
```bash
bash scripts/prod_tunnel.sh      # eigen venster; Claude: OCD_TUNNEL_SLEUTEL=~/.ssh/claude-richard-locuslegis-hetzner, op de achtergrond
set -a; . ./.env; set +a
powershell -File scripts/sync-hertaling-to-prod.ps1 -All -ProdUrl "$PROD_DB_URL" \
    > data/hertaal-achterstand/portie-NN.prod.log 2>&1
```
Controleer in de log: `v2a.hertaling` op prod = lokaal. Sluit daarna de tunnel:
eerst de lus, dan `ssh.exe` stoppen, en natellen dat poort 15433 dicht is.

**5. Bijhouden**
- Vul de rij in §4 in: datum, aantal geladen, cache lokaal en prod, opmerkingen.
- Eén regel in het vault-log (`query | Hertaal-achterstand portie NN`) en het
  dashboard (`c:/GIT/ProjectenDashboard/projecten/OCD.md`: laatste activiteit en
  volgende stap).
- Werk dit planbestand bij in de **main-worktree** `c:/GIT/OCD-vergunningen` (de kopie
  in `c:/GIT/OCD` loopt mee zodra main daar weer wordt binnengehaald) en commit en push
  alleen de voortgang, geen data.

## 4. Voortgang

| Portie | Grootte | Datum | Geladen | Cache lokaal / prod | Opmerkingen |
|---|---|---|---|---|---|
| 1 | 3.000 (meetnacht) | 2026-11-06 | 3.000/3.000, geen herstel | 53.692 / 53.692 | 25 agents in 12 min (23:15–23:27), ~148k tokens per agent, ~3,7 mln samen; 3.000 uniek, geen sjabloon; steekproef 10/10 goed |
| 2 | 9.000 | 2026-11-07 | 9.000/9.000, geen herstel | 62.692 / 62.692 | 75 agents in ~25 min (22:41–23:06), ~148k tokens per agent, ~11 mln samen; 8.992 uniek, geen sjabloon; steekproef 9/10 goed, 1 onnauwkeurig (batch 71: "bij de genoemde plekken" waar de bron een soort bouwwerk bedoelt). Wees-ssh van portie 1 hield de tunnel een etmaal open; gebruikt en daarna gestopt |
| 3 | 9.000 | 2026-11-09 | 9.000/9.000, geen herstel | 72.640 / 72.640 | 75 agents in ~29 min (22:14–22:43), ~148k tokens per agent, ~11 mln samen; 8.988 uniek, geen sjabloon; steekproef 10/10 inhoudelijk goed, 1 kromme zin (batch 70: "mag je gebruiken voor winkels"). Startcache 63.640, 948 boven het eind van portie 2 (vermoedelijk tussentijdse sync) |
| 4 | 9.000 | | | | |
| 5 | 9.000 | | | | |
| 6 | 9.000 | | | | |
| 7 | 9.000 | | | | |
| 8 | rest (~350) | | | | |

Startstand 2026-11-06: cache lokaal 50.692 rijen, prod 50.692.

Na de meetnacht (2026-11-07): portie 1 kostte naar schatting van Richard ongeveer 2% van het
weektotaal. Portiegrootte daarom 9.000 (~6% per portie); nog 54.765 open, dus portie 2 t/m 7
vol en portie 8 de rest.

2026-11-09: Richard staat toe door te gaan tot 30% van het weektotaal, dus portie 3 t/m 5 achter elkaar
(geschat ~26%). Geen meter beschikbaar; bij een andere stand het aantal porties bijstellen. Na portie 3 nog 36.352 open.

## 5. Buiten dit plan

- **8.851 teksten langer dan 3.000 tekens** wachten op de keuze in vault-gap G-179
  (afkappen, opknippen of hoger plafond). `--max-lengte 3000` houdt ze buiten de
  porties, zodat er geen nieuwe afgekapte hertalingen bijkomen.
- **Inactieve regelingen** (2.646 teksten) slaat de export over: die toont niemand.
- **Klaar** als `hertaal_fanout_export.py --alles --max-lengte 3000` nul teksten
  meldt. Daarna onderhoudt de sync het weer, stap 6bis.
