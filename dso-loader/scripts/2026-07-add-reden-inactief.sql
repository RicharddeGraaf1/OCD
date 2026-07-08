-- 2026-07: reden van inactief-status op regeling-expressies.
--
-- Achtergrond: `inactief` (2026-05) verborg tot nu toe niets voor verouderde
-- regelingversies — de vlag werd nergens gezet behalve handmatig voor 8
-- ingetrokken regelingen. We gaan hem nu óók gebruiken om verdrongen
-- expressies (oudere versies van hetzelfde frbr_work) standaard te verbergen.
-- Om die twee betekenissen uit elkaar te houden komt er een reden bij:
--   'ingetrokken'       = hele regeling vervallen/ingetrokken (geen vigerende versie)
--   'verouderde-versie' = verdrongen door een nieuwere expressie van hetzelfde work
--
-- OW-objecten/annotaties (juridische_regel, activiteit_locatieaanduiding,
-- tekst_element) krijgen GEEN eigen vlag: ze erven de status via hun
-- regeling_expression → retrieval-joins filteren met `AND NOT r.inactief`.

ALTER TABLE p2p.regeling
    ADD COLUMN IF NOT EXISTS reden_inactief TEXT NULL;

-- Bestaande inactieve regelingen waren allemaal intrekkingen.
UPDATE p2p.regeling
   SET reden_inactief = 'ingetrokken'
 WHERE inactief AND reden_inactief IS NULL;
