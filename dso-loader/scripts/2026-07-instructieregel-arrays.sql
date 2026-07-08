-- Instructieregel-annotaties zijn IMOW 0..* (meerwaardig). De kolommen waren scalar TEXT,
-- waardoor (a) api_loader de meervoudige DSO-JSON-keys niet las (alles NULL voor Bkl/AMvB)
-- en (b) het XML-pad alleen de eerste waarde bewaarde. Migreren naar text[].
ALTER TABLE p2p.juridische_regel
  ALTER COLUMN instructieregel_instrument TYPE text[]
    USING (CASE WHEN instructieregel_instrument IS NULL THEN NULL
                ELSE ARRAY[instructieregel_instrument] END),
  ALTER COLUMN instructieregel_taakuitoefening TYPE text[]
    USING (CASE WHEN instructieregel_taakuitoefening IS NULL THEN NULL
                ELSE ARRAY[instructieregel_taakuitoefening] END);
