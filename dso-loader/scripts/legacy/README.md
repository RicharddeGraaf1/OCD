# Legacy load scripts

Eenmalige batch-load scripts die hier zijn gearchiveerd. Vervangen door de
keten-pipeline in [`src/pipeline/`](../../src/pipeline/) — zie de root-README.

## Waarom hier bewaard

Deze scripts hebben hardgecodeerde bronhouder-lijsten waarmee de OCD-database
historisch is gevuld. Ze blijven nuttig als referentie:

- *welke* bronhouders zijn op welk moment geladen
- *in welke volgorde* en met welke loaders
- als blueprint voor ad-hoc backfill van een specifieke groep

De scripts gebruiken de nieuwe schema-prefixen (de loaders die ze aanroepen
zijn al gequalificeerd). Ze draaien dus nog, maar de gewone weg is voortaan:

```bash
python -m src.cli pipeline all --file mijn_bronhouders.json
```

## Inhoud

| Script | Inhoud |
|---|---|
| `load_20_extra.py` | 20 extra gemeenten, alle ketens |
| `load_30_extra.py` | 30 extra gemeenten, alle ketens |
| `load_40_extra.py` | 40 extra gemeenten, alle ketens, met timing |
| `load_50.py` | 48 bevoegde gezagen via API pipeline (G4 + grote gemeenten + provincies) |
| `load_all_ow_imtr.py` | Alle resterende provincies + waterschappen (geen Wro) |
| `load_imtr_50.py` | IMTR voor 50 gemeenten (alleen i2a) |
| `load_wro_40.py` | Wro plannen voor 40 gemeenten |
| `load_wro_40b.py` | Wro vervolg-batch |
| `load_wro_all.py` | Wro voor alle gemeenten in één pass |
