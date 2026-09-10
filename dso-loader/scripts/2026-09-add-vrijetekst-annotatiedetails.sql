-- Vrijetekst-annotaties: idealisatie, divisiesoort en de wId-brug
--
-- Aanleiding: bij het toetsen van 537 omgevingsvisies en programma's tegen de
-- Annotatierichtlijn Omgevingsvisie en Programma bleek dat drie gegevens die de
-- bron wél levert, door onze loader werden weggegooid. Zie
-- docs/vrijetekst-gaten-plan.md (V-1, V-2, V-3).
--
-- Idempotent; veilig meermaals te draaien.

-- ── V-1: idealisatie per tekstdeel ─────────────────────────────────────────
-- De Presenteren-API levert per tekstdeel {"idealisatie": {"waarde": "..."}} en
-- de XML-parser leest het al uit; er was alleen geen kolom om het in te zetten.
-- Van de omgevingsvisie van Waadhoeke staan alle 76 tekstdelen op "indicatief":
-- die hele visie is als indicatief bedoeld en dat was nergens terug te vinden.
ALTER TABLE p2p.tekstdeel
    ADD COLUMN IF NOT EXISTS idealisatie TEXT NULL REFERENCES core.idealisatie(code);

COMMENT ON COLUMN p2p.tekstdeel.idealisatie IS
    'exact | indicatief. Geldt voor de Locatie(s) van dit tekstdeel; dezelfde '
    'Locatie kan bij een ander tekstdeel een andere idealisatie hebben. '
    'NULL = niet geladen (voorraad van voor 2026-09-10), niet "onbekend".';

-- ── V-2: annoteert dit tekstdeel een divisietekst of een divisie? ──────────
-- De API-route las alleen `divisietekstRef`. Een tekstdeel dat een divisie
-- annoteert heeft in plaats daarvan `divisieRef` en kreeg dus een lege
-- divisie_wid: 2.934 rijen die eruitzagen als een kapotte verwijzing terwijl
-- het annotaties op divisieniveau zijn. Dat onderscheid hoort in de data te
-- staan, niet afgeleid te worden uit de vorm van de identificatie.
ALTER TABLE p2p.tekstdeel
    ADD COLUMN IF NOT EXISTS divisie_soort TEXT NULL;

ALTER TABLE p2p.tekstdeel DROP CONSTRAINT IF EXISTS tekstdeel_divisie_soort_check;
ALTER TABLE p2p.tekstdeel
    ADD CONSTRAINT tekstdeel_divisie_soort_check
    CHECK (divisie_soort IS NULL OR divisie_soort IN ('divisietekst', 'divisie'));

COMMENT ON COLUMN p2p.tekstdeel.divisie_soort IS
    'Waar hangt deze annotatie aan: divisietekst of divisie. Annoteren op '
    'alleen divisieniveau is toegestaan maar wordt afgeraden: Regels op de '
    'kaart toont zo n annotatie niet bij de onderliggende divisieteksten.';

-- ── V-3: de brug tussen IMOW-annotatie en STOP-tekst ───────────────────────
-- Dezelfde API-respons bevat `divisies` en `divisieteksten`, elk met hun wId.
-- Dat is exact de koppeling die ontbrak: zonder deze tabel kun je alleen op
-- documentniveau tellen hoeveel divisieteksten geannoteerd zijn, en niet welke.
CREATE TABLE IF NOT EXISTS p2p.divisie (
    identificatie       TEXT PRIMARY KEY,
    wid                 TEXT NOT NULL,
    soort               TEXT NOT NULL,
    -- Bewust geen FK naar p2p.regeling: net als bij de andere IMOW-objecten mag
    -- een expressie verdwijnen zonder het object mee te nemen.
    regeling_expression TEXT NULL
);

ALTER TABLE p2p.divisie DROP CONSTRAINT IF EXISTS divisie_soort_check;
ALTER TABLE p2p.divisie
    ADD CONSTRAINT divisie_soort_check
    CHECK (soort IN ('divisietekst', 'divisie'));

CREATE INDEX IF NOT EXISTS divisie_wid_idx ON p2p.divisie (wid);
CREATE INDEX IF NOT EXISTS divisie_expr_idx ON p2p.divisie (regeling_expression);

COMMENT ON TABLE p2p.divisie IS
    'IMOW-divisie(tekst) met zijn STOP-wId. Maakt de keten p2p.tekstdeel -> '
    'p2p.divisie -> p2p.tekst_element sluitend, zodat een annotatie aan het '
    'tekstelement gekoppeld kan worden waar hij bij hoort. Gevuld sinds '
    '2026-09-10 uit de collecties divisies/divisieteksten van de '
    'divisieannotaties-respons.';
