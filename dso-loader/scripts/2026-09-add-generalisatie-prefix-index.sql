-- Prefix-index op identificatie, zodat een herbouw per bronhouder de index kan
-- gebruiken in plaats van de hele tabel te scannen.
--
-- Waarom text_pattern_ops en niet de gewone btree die er al staat: de database
-- draait op collatie en_US.utf8, en een btree met de standaard-opclass is
-- geordend volgens die collatie. Daardoor werkt geen van de twee voor de hand
-- liggende prefix-vormen:
--
--   * `identificatie LIKE 'nl.imow-gm0995.%'` -- de planner kan onder een
--     niet-C-collatie niet bewijzen dat dit een prefix-bereik is en valt terug
--     op een volledige parallelle scan. Gemeten 2026-08-31: kosten 1.645.474
--     tegen 21, goed voor ~14 s vaste voet per bronhouder ook bij 24 rijen.
--
--   * `identificatie >= 'nl.imow-gm0995.' AND < 'nl.imow-gm0995/'` -- geeft het
--     VERKEERDE ANTWOORD. en_US weegt leestekens licht, waardoor
--     `nl.imow-gm0279.ambtsgebied...` buiten dat bereik valt. Gemeten: 0 rijen
--     waar er 8 zijn. Dit is de gevaarlijkste van de twee, want hij is snel én
--     stil fout.
--
-- Met `COLLATE "C"` in het predicaat plus deze index klopt het antwoord én is
-- het bereik indexeerbaar: in byte-orde is `.` 0x2E en `/` 0x2F, dus alles wat
-- met `nl.imow-<code>.` begint valt er precies tussen.

CREATE INDEX IF NOT EXISTS idx_locatie_subdiv_id_prefix
    ON p2p.locatie_subdiv (identificatie COLLATE "C" text_pattern_ops);

CREATE INDEX IF NOT EXISTS idx_locatie_gen_id_prefix
    ON p2p.locatie_generalisatie (identificatie COLLATE "C" text_pattern_ops, niveau);

CREATE INDEX IF NOT EXISTS idx_planobject_id_prefix
    ON wro.planobject (identificatie COLLATE "C" text_pattern_ops);
