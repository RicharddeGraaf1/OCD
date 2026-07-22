-- ============================================================================
-- 2026-07 · PROD-VEILIGE base-DDL voor de hertaling-cache (begrijpelijke variant).
--
-- Spiegel van scripts/2026-07-add-hertaling-cache.sql MAAR zonder de lokale
-- migratie-INSERT uit v2a.tekst_begrijpelijk en zonder DROP — die tabel bestaat
-- niet op prod en de data komt via COPY-upsert (sync-hertaling-to-prod.ps1).
--
-- Idempotent: CREATE SCHEMA/FUNCTION/TABLE ... IF NOT EXISTS / OR REPLACE.
-- Draai daarna scripts/2026-07-add-element-hash-koppeling.sql voor MV + view.
--
-- Run (via tijdelijke Railway TCP-proxy):
--   psql "$ProdUrl" -v ON_ERROR_STOP=1 -f scripts/2026-07-hertaling-prod-base.sql
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS v2a;

-- Normalisatie-hash: één bron van waarheid (identiek aan lokaal). Trim +
-- spaties samenvouwen zodat whitespace-only-kopieën samenvallen. IMMUTABLE
-- STRICT is vereist zodat de MV-uitdrukking indexeerbaar/stabiel is.
CREATE OR REPLACE FUNCTION v2a.norm_hash(t text) RETURNS text
  LANGUAGE sql IMMUTABLE STRICT AS
$$ SELECT md5(regexp_replace(btrim(t), '\s+', ' ', 'g')) $$;

CREATE TABLE IF NOT EXISTS v2a.hertaling (
    bron_hash      TEXT NOT NULL,             -- v2a.norm_hash(inhoud_plain)
    model          TEXT NOT NULL,             -- 'claude-sonnet-5' | 'claude-haiku-4-5' | ...
    prompt_versie  TEXT NOT NULL,
    tekst          TEXT NOT NULL,             -- de begrijpelijke variant (context-vrij)
    status         TEXT NOT NULL DEFAULT 'geen-juridische-status',
    gegenereerd_op TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (bron_hash, model, prompt_versie)
);

COMMENT ON TABLE v2a.hertaling IS
    'Content-adresseerbare cache van LLM-hertalingen (begrijpelijke variant) per '
    'UNIEKE regeltekst. Gekeyd op v2a.norm_hash(inhoud_plain) → dedupt de '
    'bruidsschat-kopieën over gemeenten. GEEN juridische status.';
