-- Bestanden die na een lange pauze definitief 503 bleven geven.
-- Apart van i2a.sttr_bestand omdat het géén inhoud is maar een bevinding:
-- de RTR biedt een sttrBestand aan dat hij niet kan leveren. Dat is een
-- dekkingsgat in de bron, en dat willen we kunnen tellen en melden.
CREATE TABLE IF NOT EXISTS i2a.sttr_bestand_mislukt (
    sttr_id      text PRIMARY KEY,
    fsr          text,
    oin          text,
    pogingen     int         NOT NULL DEFAULT 1,
    laatste_code int,
    laatst_op    timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE i2a.sttr_bestand_mislukt IS
  'sttrBestanden die herhaald 503 gaven. Na 3 pogingen slaat de backfill ze '
  'over, zodat één kapot bestand niet elke nacht de run kost (gaps#G-138).';
