-- ============================================================================
-- 2026-07 · v2a.tekst_begrijpelijk — begrijpelijke variant (hertaling) per element.
--
-- Ontwerp: vault_v1/analysis/Begrijpelijke variant lokaal in OCD - RoM-PoC Broekhem 33.md
-- (niveau 2 van vault_v1/analysis/Generiek leesmodel en STOP-weergavecomponent.md §8).
--
-- LLM-gegenereerde, NIET-juridische hertaling van een content-element (Lid/
-- Divisietekst/Begrip). Afgeleid + herbouwbaar → v2a, niet p2p. Spiegelt de
-- omgevingsbot simplify-prompt, maar geprecompute + gecached i.p.v. per-view.
--
-- Populatie via scripts/begrijpelijk-broekhem33.py (Ollama-Qwen, idempotent).
-- Idempotent: CREATE TABLE IF NOT EXISTS + CREATE INDEX IF NOT EXISTS.
--
-- Run:  psql -h localhost -p 5434 -d dso -f scripts/2026-07-add-tekst-begrijpelijk.sql
-- ============================================================================

CREATE TABLE IF NOT EXISTS v2a.tekst_begrijpelijk (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tekst_element_id    BIGINT NOT NULL REFERENCES p2p.tekst_element(id) ON DELETE CASCADE,
    wid                 TEXT   NOT NULL,             -- join-key voor afnemers; niet globaal uniek → met regeling_expression
    regeling_expression TEXT   NOT NULL,             -- versie-scharnier: nieuwe versie = nieuwe expression = hergenereren
    tekst               TEXT   NOT NULL,             -- de begrijpelijke variant (hertaling)
    model               TEXT   NOT NULL,             -- 'qwen2.5:14b' | 'claude-sonnet-4-6'
    prompt_versie       TEXT   NOT NULL,             -- bump → nieuwe variant naast de oude
    bron_hash           TEXT   NOT NULL,             -- md5(inhoud_plain) → invalidatie bij tekst-drift
    status              TEXT   NOT NULL DEFAULT 'geen-juridische-status',
    gegenereerd_op      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tekst_element_id, model, prompt_versie)
);

-- (tekst_element_id-lookups zijn al gedekt door de UNIQUE-prefix hierboven.)
CREATE INDEX IF NOT EXISTS idx_tekst_begrijpelijk_wid
    ON v2a.tekst_begrijpelijk (regeling_expression, wid);

COMMENT ON TABLE v2a.tekst_begrijpelijk IS
    'LLM-gegenereerde begrijpelijke variant (hertaling) per content-element. '
    'Afgeleid/herbouwbaar, GEEN juridische status — zie de brontekst in '
    'p2p.tekst_element.inhoud. Concrete DB-vorm van het leesmodel-samenvattingveld.';
COMMENT ON COLUMN v2a.tekst_begrijpelijk.bron_hash IS
    'md5 van inhoud_plain bij generatie. Mismatch = brontekst gewijzigd → hergenereren.';
