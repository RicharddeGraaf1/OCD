-- ============================================================================
-- 2026-07 · v2a.hertaling — CONTENT-ADRESSEERBARE begrijpelijke-variant-cache.
--
-- Ontwerp: vault_v1/analysis/Begrijpelijke variant lokaal in OCD - RoM-PoC Broekhem 33.md
--
-- De hertaling hoort bij de TEKST, niet bij het element. We hashen de
-- genormaliseerde inhoud_plain en cachen per UNIEKE tekst. De bruidsschat heeft
-- dezelfde regeltekst in ~342 gemeenten gekopieerd → gemeten dedup 3,87×
-- (391.267 elementen → 101.029 unieke teksten). Zo kost een standaardzin één
-- LLM-call i.p.v. honderden, en is de hertaling compounding over gemeenten.
--
-- Vervangt de element-gekeyde v2a.tekst_begrijpelijk (dedupt hierin, dan drop).
-- Idempotent: CREATE ... IF NOT EXISTS + ON CONFLICT DO NOTHING.
--
-- Run:  psql -h localhost -p 5434 -d dso -f scripts/2026-07-add-hertaling-cache.sql
-- ============================================================================

-- Normalisatie-hash: één bron van waarheid. Trim + spaties samenvouwen zodat
-- kopieën die alleen in whitespace verschillen alsnog samenvallen.
CREATE OR REPLACE FUNCTION v2a.norm_hash(t text) RETURNS text
  LANGUAGE sql IMMUTABLE STRICT AS
$$ SELECT md5(regexp_replace(btrim(t), '\s+', ' ', 'g')) $$;

CREATE TABLE IF NOT EXISTS v2a.hertaling (
    bron_hash      TEXT NOT NULL,             -- v2a.norm_hash(inhoud_plain)
    model          TEXT NOT NULL,             -- 'claude-sonnet-5' | 'claude-haiku-4-5' | 'qwen2.5:14b' | ...
    prompt_versie  TEXT NOT NULL,
    tekst          TEXT NOT NULL,             -- de begrijpelijke variant (context-vrij)
    status         TEXT NOT NULL DEFAULT 'geen-juridische-status',
    gegenereerd_op TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (bron_hash, model, prompt_versie)
);

COMMENT ON TABLE v2a.hertaling IS
    'Content-adresseerbare cache van LLM-hertalingen (begrijpelijke variant) per '
    'UNIEKE regeltekst. Gekeyd op v2a.norm_hash(inhoud_plain) → dedupt de '
    'bruidsschat-kopieën over gemeenten. Afnemers joinen per element via de hash. '
    'GEEN juridische status — zie brontekst p2p.tekst_element.inhoud.';

-- Migreer bestaande element-gekeyde rijen → content-cache (dedup op norm_hash).
-- DISTINCT ON kiest de nieuwste hertaling per (norm_hash, model, prompt).
INSERT INTO v2a.hertaling (bron_hash, model, prompt_versie, tekst, status, gegenereerd_op)
SELECT DISTINCT ON (v2a.norm_hash(te.inhoud_plain), tb.model, tb.prompt_versie)
       v2a.norm_hash(te.inhoud_plain), tb.model, tb.prompt_versie,
       tb.tekst, tb.status, tb.gegenereerd_op
FROM v2a.tekst_begrijpelijk tb
JOIN p2p.tekst_element te ON te.id = tb.tekst_element_id
ORDER BY v2a.norm_hash(te.inhoud_plain), tb.model, tb.prompt_versie, tb.gegenereerd_op DESC
ON CONFLICT (bron_hash, model, prompt_versie) DO NOTHING;

-- Oude element-gekeyde tabel is nu volledig gedekt door de content-cache.
DROP TABLE IF EXISTS v2a.tekst_begrijpelijk;
