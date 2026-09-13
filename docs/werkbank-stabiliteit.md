# Werkbank-stabiliteit — voorkomen dat de sync onder je handen wegvalt

*Opgesteld 2026-09-13, na de sync van 12/13 september.*

Dit document beschrijft **twee instellingen die je zelf op de machine moet
zetten** en één codewijziging die al gedaan is. Ze horen bij elkaar: de
instellingen maken de uitval onwaarschijnlijker, de code zorgt dat je het
mérkt als hij tóch gebeurt.

---

## Wat er gebeurde

De sync van 2026-09-12 startte om 23:49 en draaide zijn preflight groen:

```
[23:51:38] Preflight ok: DB 96 GB, 2055 regelingen, 444 GB vrij
```

Daarna, uit het Windows-eventlog (Application):

```
23:52:32  MsiInstaller    start ...WindowsSubsystemForLinux_2.7.14.0...\wsl.msi
23:52:36  RestartManager  kan wsl.exe / wslhost.exe / vmwp.exe niet herstarten
23:52:44  full_sync       p2p delta-sweep (volledige lijst) over 381 bronhouders
```

De **Microsoft Store werkte WSL bij naar 2.7.14.0** en `wsl.msi` legde daarbij
de VM-worker om — en dus de `dso-postgis`-container. De delta-sweep begon acht
seconden later tegen een dode database en liep **119 bronhouders lang** tegen
`connection timeout expired`, elk netjes opgevangen en gelogd, zonder dat de
fase stopte.

Drie dingen die het erger maakten dan nodig:

1. De **preflight draait één keer**, vóór de sweep. Hij was groen; de bodem
   zakte er ná de check onderuit.
2. De **delta-lus ving elke fout op en ging door**. Terecht voor één bronhouder
   met rommel, fataal voor een dode database.
3. **Docker Desktop startte niet vanzelf terug.** De container heeft
   `restart: unless-stopped` en kwam netjes terug zodra de engine er weer was —
   dat deel werkte. Maar de engine zelf moest met de hand aan.

Postgres deed daarna crash recovery en kwam schoon terug: 0 weesrijen in de
hele database. Er is geen data verloren gegaan.

---

## 1. Microsoft Store — automatische app-updates uit

**Waarom.** WSL is sinds enige tijd een Store-app. De Store werkt apps 's
nachts bij, in precies het venster waarin de sync draait. Dit is niet iets wat
Docker Desktop of Postgres kan opvangen: de VM waarin ze draaien wordt onder ze
weggehaald.

**Doen:**

1. Open de **Microsoft Store**.
2. Klik rechtsboven op je **profielafbeelding** → **Instellingen**.
3. Zet **App-updates** (*"App updates" / "Automatische app-updates"*) **uit**.

**Gevolg.** Je moet voortaan zelf bijwerken. Dat is de bedoeling — je wilt
kiezen wannéér WSL herstart, niet of het gebeurt.

**Hoort erbij: werk WSL bewust bij vóór een sync, niet erna.**

```powershell
wsl --update
wsl --shutdown          # alleen als de update daarom vraagt
```

Daarna Docker Desktop laten opkomen en pas dan de sync starten. Een update die
je zelf om 20:00 doet kost vijf minuten; dezelfde update om 23:52 kost een
halve nacht.

**Controleren of het gelukt is:**

```powershell
Get-AppxPackage MicrosoftCorporationII.WindowsSubsystemForLinux |
  Select-Object Name, Version
```

Noteer die versie. Verandert hij zonder dat jij `wsl --update` hebt gedraaid,
dan staan de auto-updates nog aan.

---

## 2. Docker Desktop — automatisch starten aan

**Waarom.** Nu staat hij uit:

```
AutoStart : False        (C:\Users\RdG2\AppData\Roaming\Docker\settings-store.json)
```

De container komt vanzelf terug (`restart: unless-stopped`, geverifieerd), maar
alleen als de engine draait. Staat `AutoStart` uit, dan blijft de database weg
tot iemand Docker Desktop handmatig start — precies wat op 13 september
gebeurde.

**Doen:**

1. Open **Docker Desktop** → tandwiel (**Settings**) → **General**.
2. Vink aan: **"Start Docker Desktop when you sign in to your computer"**.
3. **Apply & restart**.

**Controleren:**

```powershell
$s = 'C:\Users\RdG2\AppData\Roaming\Docker\settings-store.json'
(Get-Content $s -Raw | ConvertFrom-Json).AutoStart      # moet True zijn
```

> **Let op wat dit wél en niet oplost.** Dit dekt het geval "machine is
> herstart". Het dekt níet het geval van 12 september, waarin de engine
> midden in een sessie omviel zonder herstart. Daarvoor is punt 1 de echte
> maatregel en punt 3 het vangnet.

---

## 3. Al gedaan — de noodrem in de loader

Dit hoef je niet zelf te doen; het staat er al. Wel goed om te weten dat het
er is, want het verandert hoe een storing zich voordoet.

`src/loaders/api_loader.py` telt nu opeenvolgende **verbindingsfouten** en
breekt de sweep af bij vijf op rij:

```python
MAX_OPEENVOLGENDE_DB_FOUTEN = 5

class DatabaseOnbereikbaar(RuntimeError):
    ...
```

- Alleen `psycopg.OperationalError` telt mee, inclusief ingepakt in een andere
  exception (de oorzaakketen wordt afgelopen).
- **DSO-rommel bij één bronhouder telt niet mee** en stopt de sweep dus nooit —
  dat gedrag blijft precies zoals het was.
- Een geslaagde bronhouder zet de teller terug op nul.

Gevolg voor 12 september: de sweep was gestopt bij bronhouder 5 in plaats van
door te malen tot 120, met een leesbare fout in plaats van 119 rode regels.

Herstarten is goedkoop — de skip-guard herkent een al geladen expressie in
~1,1 ms, dus een herstart na herstel kost seconden, geen uren.

Tests: `dso-loader/tests/test_db_noodrem.py` (7 tests).

```bash
cd dso-loader && python -m pytest tests/test_db_noodrem.py -q
```

---

## Losse bevinding — een stale test

`tests/test_imtr_peildatum.py::test_geen_hardgecodeerde_datum_meer` faalt, en
al vóór het bovenstaande werk:

```
assert bron.count('"datum": _peildatum()') == 3
E       assert 2 == 3
```

Commit `590b2a0` ("werkzaamheid-junctie, beleefde client en de
vergunningcheck-endpoints") verwijderde het hele `activiteitKoppelingen`-blok,
inclusief zijn `_peildatum()`-call. Er hóren er dus nog twee te zijn; de test
telt nog naar de oude situatie.

Dit is een verouderde assertie, **geen regressie op de peildatum** — de twee
resterende call-sites gebruiken `_peildatum()` netjes. De fix is het getal in
de test op 2 zetten, met een regel erbij waarom het er drie waren. Bewust nog
niet gedaan: het viel buiten de sync waarin het opviel.

---

## Verwijzingen

- [synchronisatie-runbook.md](synchronisatie-runbook.md) — §4 *Afbreken &
  herstel* beschrijft hoe je een afgebroken run opruimt (`core.load_run` op
  `running`, `audit.sync_run` zonder `klaar_op`).
- De twee afgebroken runs van 12/13 september staan in `audit.sync_run` met
  `opmerking` beginnend met *"afgebroken"* — dat is wat
  `repliceer_p2p_naar_prod.py` gebruikt om ze niet als "laatste geslaagde sync"
  te pakken.
