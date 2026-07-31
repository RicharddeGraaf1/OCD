-- ══════════════════════════════════════════════════════════════════
-- Fase 1 sub 1.0 — Schema voor juridische_regel-delta en bindingen.
--
-- Waarom: annotaties (activiteit / norm / gebiedsaanwijzing) hangen
-- niet direct aan artikel-tekst. De keten loopt via juridische_regel:
--   annotatie → juridische_regel → regeltekst_wid → tekst_element
--   → parent-chain naar 'artikel' element_type.
--
-- Zonder deze tabellen kunnen we voor een annotatie-delta niet
-- afleiden aan welk artikel hij hangt — vooral bij scenario waar
-- alleen een binding wijzigt en de regeltekst ongewijzigd blijft
-- (dan is er géén tekst_element-delta die de artikel-context geeft).
--
-- Design-keuzes:
--   * juridische_regel_delta heeft GEEN bewerking-kolom: een regel
--     "bestaat" in een ontwerp omdat z'n tekst of z'n bindingen
--     wijzigen — die lifecycle leeft in tekst_element resp. de
--     binding-tabellen hieronder.
--   * bewerking zit op elke BINDING apart: een gewijzigde regel kan
--     tegelijk activiteit X toevoegen én norm Y verwijderen (twee
--     onafhankelijke lifecycles op dezelfde regel).
--   * Doel-identificaties zijn TEXT zonder FK naar p2p — een
--     nieuwe activiteit in dit ontwerp bestaat mogelijk nog niet in
--     de geconsolideerde p2p-tabellen.
--   * Voor activiteit-bindingen slaan we ook `locatie_identificatie`
--     op, spiegelend aan p2p.activiteit_locatieaanduiding (de locatie
--     is onderdeel van de binding-identiteit).
-- ══════════════════════════════════════════════════════════════════

BEGIN;

-- Regel-meta per ontwerp. Bestaat zodra de regel op een of andere
-- manier in dit ontwerp wordt geraakt (tekst-wijziging of binding-
-- wijziging). regeltekst_wid koppelt terug naar tekst_element voor
-- de artikel-parent-chain.
CREATE TABLE IF NOT EXISTS p2pwijziging.juridische_regel_delta (
    identificatie        TEXT NOT NULL,
    ontwerpbesluit_id    TEXT NOT NULL
        REFERENCES p2pwijziging.besluit(ontwerpbesluit_id) ON DELETE CASCADE,
    regeltekst_wid       TEXT NOT NULL,
    PRIMARY KEY (identificatie, ontwerpbesluit_id)
);
CREATE INDEX IF NOT EXISTS idx_pw_jr_besluit
    ON p2pwijziging.juridische_regel_delta(ontwerpbesluit_id);
CREATE INDEX IF NOT EXISTS idx_pw_jr_wid
    ON p2pwijziging.juridische_regel_delta(regeltekst_wid);


-- ── Activiteit-binding (spiegelt p2p.activiteit_locatieaanduiding) ──
-- locatie_identificatie hoort bij de binding-identiteit: dezelfde
-- (regel, activiteit) kan aan meerdere locaties koppelen.
CREATE TABLE IF NOT EXISTS p2pwijziging.juridische_regel_activiteit_delta (
    id                             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ontwerpbesluit_id              TEXT NOT NULL
        REFERENCES p2pwijziging.besluit(ontwerpbesluit_id) ON DELETE CASCADE,
    juridische_regel_identificatie TEXT NOT NULL,
    activiteit_identificatie       TEXT NOT NULL,
    locatie_identificatie          TEXT,
    bewerking                      TEXT NOT NULL
        CHECK (bewerking IN ('toevoegen', 'wijzigen', 'verwijderen')),
    UNIQUE (ontwerpbesluit_id, juridische_regel_identificatie,
            activiteit_identificatie, locatie_identificatie),
    FOREIGN KEY (juridische_regel_identificatie, ontwerpbesluit_id)
        REFERENCES p2pwijziging.juridische_regel_delta(identificatie, ontwerpbesluit_id)
        ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_pw_jr_act_besluit
    ON p2pwijziging.juridische_regel_activiteit_delta(ontwerpbesluit_id);
CREATE INDEX IF NOT EXISTS idx_pw_jr_act_activiteit
    ON p2pwijziging.juridische_regel_activiteit_delta(activiteit_identificatie);
CREATE INDEX IF NOT EXISTS idx_pw_jr_act_locatie
    ON p2pwijziging.juridische_regel_activiteit_delta(locatie_identificatie)
    WHERE locatie_identificatie IS NOT NULL;


-- ── Norm-binding (spiegelt p2p.juridische_regel_norm) ──
CREATE TABLE IF NOT EXISTS p2pwijziging.juridische_regel_norm_delta (
    id                             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ontwerpbesluit_id              TEXT NOT NULL
        REFERENCES p2pwijziging.besluit(ontwerpbesluit_id) ON DELETE CASCADE,
    juridische_regel_identificatie TEXT NOT NULL,
    norm_identificatie             TEXT NOT NULL,
    bewerking                      TEXT NOT NULL
        CHECK (bewerking IN ('toevoegen', 'wijzigen', 'verwijderen')),
    UNIQUE (ontwerpbesluit_id, juridische_regel_identificatie, norm_identificatie),
    FOREIGN KEY (juridische_regel_identificatie, ontwerpbesluit_id)
        REFERENCES p2pwijziging.juridische_regel_delta(identificatie, ontwerpbesluit_id)
        ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_pw_jr_norm_besluit
    ON p2pwijziging.juridische_regel_norm_delta(ontwerpbesluit_id);
CREATE INDEX IF NOT EXISTS idx_pw_jr_norm_norm
    ON p2pwijziging.juridische_regel_norm_delta(norm_identificatie);


-- ── Gebiedsaanwijzing-binding (spiegelt p2p.juridische_regel_gebiedsaanwijzing) ──
CREATE TABLE IF NOT EXISTS p2pwijziging.juridische_regel_gebiedsaanwijzing_delta (
    id                             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ontwerpbesluit_id              TEXT NOT NULL
        REFERENCES p2pwijziging.besluit(ontwerpbesluit_id) ON DELETE CASCADE,
    juridische_regel_identificatie TEXT NOT NULL,
    gebiedsaanwijzing_identificatie TEXT NOT NULL,
    bewerking                      TEXT NOT NULL
        CHECK (bewerking IN ('toevoegen', 'wijzigen', 'verwijderen')),
    UNIQUE (ontwerpbesluit_id, juridische_regel_identificatie,
            gebiedsaanwijzing_identificatie),
    FOREIGN KEY (juridische_regel_identificatie, ontwerpbesluit_id)
        REFERENCES p2pwijziging.juridische_regel_delta(identificatie, ontwerpbesluit_id)
        ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_pw_jr_ga_besluit
    ON p2pwijziging.juridische_regel_gebiedsaanwijzing_delta(ontwerpbesluit_id);
CREATE INDEX IF NOT EXISTS idx_pw_jr_ga_ga
    ON p2pwijziging.juridische_regel_gebiedsaanwijzing_delta(gebiedsaanwijzing_identificatie);


-- Sanity: tel tabellen na aanmaken (moeten leeg zijn — worden gevuld door sub 1.1).
SELECT
  (SELECT COUNT(*) FROM p2pwijziging.juridische_regel_delta) AS regels,
  (SELECT COUNT(*) FROM p2pwijziging.juridische_regel_activiteit_delta) AS act_bindings,
  (SELECT COUNT(*) FROM p2pwijziging.juridische_regel_norm_delta) AS norm_bindings,
  (SELECT COUNT(*) FROM p2pwijziging.juridische_regel_gebiedsaanwijzing_delta) AS ga_bindings;

COMMIT;
