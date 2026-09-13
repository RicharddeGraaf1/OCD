# Waarom `enrich-koop` 2,5 uur duurt — gemeten, 2026-09-13

*Aanleiding: de vth-verrijking is met afstand de langste stap van een gewone
sync. De aanname was dat het ophalen van 6.714 publicaties over een trage lijn
nu eenmaal duurt, en dat parallelliseren of lokaal cachen de oplossing zou zijn.
Beide blijken onjuist.*

---

## De meting

Alles hieronder is gemeten tijdens de sync van 13-09, met de enricher draaiend.

| Component | Gemeten |
|---|---|
| Ophalen van één publicatie-XML | **~30 ms** (koud én warm) |
| Grootte van zo'n document | mediaan **2 KB**, max 5 KB |
| Volledige parse + alle extracties | **0,3 ms** per document |
| Postgres-backend | **60 van 60 metingen** `idle in transaction`, wachtend op de client |
| Python-proces | **0,3 % van één kern** over 10 s wandklok |
| Feitelijk tempo | **1,2 s per record** |

Fetch + parse + de ingebouwde `time.sleep(0.3)` is samen ~330 ms. Er ontbrak dus
ruim 850 ms per record, en zowel de database als de CPU deed in die tijd niets.

## Waar die 850 ms heen gaat

Het tempoverloop over de hele run wijst het aan:

```
07:15   50
07:16  150
07:17  200      ← 3,3 req/s: precies waar REQUEST_INTERVAL=0.3 op mikt
07:18   50
...
09:02   50      ← 100+ minuten onafgebroken exact 50/min
```

De eerste minuten haalt de enricher zijn ontwerptempo. Daarna zakt hij naar een
kwart daarvan en blijft daar tot het eind. Dat is het profiel van een
**token-bucket aan de serverkant die vol begint en daarna bijvult op de
werkelijke limiet**.

Wat er dan gebeurt, in [koop_vergunning.py:1031-1046](../dso-loader/src/loaders/koop_vergunning.py#L1031-L1046):
KOOP antwoordt met **HTTP 429**, `raise_for_status()` maakt daar een
`httpx.HTTPError` van, de retry-lus vangt hem en slaapt — en `RETRY_BACKOFF_SEQ`
begint op **5 seconden**.

Reken het na: om van 180/min (0,33 s per record) naar 50/min (1,2 s) te zakken
is ~0,87 s extra per record nodig. Bij een vaste boete van 5 s betekent dat
ongeveer **één 429 per zes requests**. De enricher gaat dus te hard, wordt
teruggefloten, slaapt vijf seconden, gaat weer te hard — en middelt uit op 0,83
req/s.

## Twee controles die dit onderbouwen

**De 429 is echt en niet incidenteel.** Een burst van 60 requests zonder pauze
gaf **58 × 429** en 2 × 200.

**Maar op het tempo van de enricher zelf is er ruimte.** Na een minuut rust, 15
requests op 1,2 s afstand: **15 × 200**, geen enkele 429. De limiet ligt dus
ergens **tussen 0,83 en 3,3 req/s** — en daar zit de hele winst.

> De 58/60 uit de burst zeggen op zichzelf niets over de enricher: die burst was
> zelf de overtreding. Pas het verschil tussen die twee metingen, samen met het
> tempoverloop hierboven, maakt het beeld sluitend.

## Wat dus *niet* helpt

**Parallelliseren.** De fetch is 30 ms; er valt geen latency te verbergen. Meer
gelijktijdige requests betekent alleen sneller tegen dezelfde serverlimiet aan
lopen, en dus méér 429's.

**De XML lokaal op schijf zetten om ophalen en verwerken te scheiden.** Het
verwerken kost 0,3 ms en de database staat al te wachten; er is geen
verwerkingslast om weg te halen. Bovendien *is* de XML al gepersisteerd — in
`inhoud_xml` en `inhoud_tekst` in de database zelf. Opnieuw parsen zonder opnieuw
op te halen kan dus al qua data; er is alleen geen codepad voor (`enrich_records`
selecteert op `inhoud_geladen_at IS NULL`). Wie dat pad wil, schrijft een
`herparse`-commando dat uit `inhoud_xml` leest — geen schijfcache.

**Een batch-endpoint bij KOOP.** Elke publicatie is een eigen FRBR-resource
(`/frbr/officielepublicaties/gmb/2025/gmb-2025-134519/1/xml/…`). Of KOOP daarnaast
een bulk- of dagexport aanbiedt is **niet onderzocht** — dat is het uitzoeken
waard, maar het is geen bekend gegeven en ik heb het niet geverifieerd.

## Wat wél helpt

**Behandel 429 als een tempo-instructie, niet als een netwerkstoring.** Dat is de
kern. Concreet:

1. **Onderscheid 429 van de rest in de retry-lus.** Nu vallen een 429, een
   time-out en een 503 allemaal in dezelfde `except httpx.HTTPError` met dezelfde
   backoff-reeks `[5, 15, 45, 90, 180]`. Voor een echte storing is 5 s een
   redelijke eerste stap; voor "je gaat te hard" is het een eeuwigheid.
2. **Lees `Retry-After` als de server hem meestuurt.** In de probe hierboven is
   geen 429 gevangen, dus of KOOP die header zet is nog onbekend — dat moet je
   opvangen wanneer je er een ziet, niet vooraf aannemen.
3. **Regel het tempo adaptief in plaats van vast.** Een AIMD-limiter (bij succes
   het interval geleidelijk verkleinen, bij een 429 direct halveren) zoekt de
   werkelijke limiet vanzelf op en blijft eronder. Dat vervangt zowel de vaste
   `REQUEST_INTERVAL` als de vaste eerste backoff.

**Verwachte winst.** De sustainable rate ligt tussen 0,83 en 3,3 req/s; waar
precies is niet gemeten. Op 2 req/s zou de stap van ~2,2 uur naar ~55 minuten
gaan. Dat getal is een schatting op basis van een niet-gemeten grootheid —
**meet eerst de werkelijke limiet** met een oplopende probe (bijvoorbeeld 1,0 /
1,5 / 2,0 / 2,5 req/s, elk twee minuten, en tel de 429's) voordat je een vaste
waarde in de code zet.

## Op te ruimen bij dezelfde ingreep

De opmerking bij `REQUEST_INTERVAL` klopt niet meer:

```python
REQUEST_INTERVAL = 0.3  # ~3.3 req/sec — KOOP-vriendelijk, geen 503-throttle gemeten
```

Er is wel degelijk throttling; alleen komt hij als **429**, niet als 503. De
oorspronkelijke meting keek naar de verkeerde statuscode en concludeerde daaruit
dat er geen limiet was. Dat is dezelfde vorm als de andere stille controles in
dit project: er werd wél gemeten, maar naar iets anders dan wat er misging.

Ook meenemen: `enrich-koop` schrijft geen voortgang naar het sync-log (zie
[sync-2026-09-13-bevindingen.md](sync-2026-09-13-bevindingen.md) §7). Een teller
per N records naar stdout maakt zowel het tempo als een throttle-inzinking
zichtbaar terwijl de stap loopt, in plaats van achteraf uit de database.
