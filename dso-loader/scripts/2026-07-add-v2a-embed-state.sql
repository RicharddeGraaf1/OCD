-- ============================================================================
-- 2026-07 · v2a.embed_state — watermark/dirty-detectie voor de incrementele
--           vector-index-refresh (v2a-refresh).
--
-- Ontwerp: analysis/Synchronisatie vector-index-lagen - incrementele v2a-refresh.md
--
-- Per scope-key (regeling_expression | instrument_idn | ontwerpbesluit_id) houdt
-- deze tabel de content-hash bij van de laatst-geëmbedde brontekst. Dirty =
-- nieuw OF hash veranderd. Content-hash i.p.v. id/timestamp, want p2p-herlaad is
-- UPSERT-DO-NOTHING (gewijzigde inhoud, ongewijzigd id) en de ontwerp/herlaad-
-- paden regenereren serial-id's — alleen een hash is loader-pad-agnostisch.
-- ============================================================================

CREATE TABLE IF NOT EXISTS v2a.embed_state (
    scope_key     TEXT NOT NULL,        -- regeling_expression | instrument_idn | ontwerpbesluit_id
    source_type   TEXT NOT NULL,        -- 'p2p' | 'wro' | 'ontwerp'
    content_hash  TEXT NOT NULL,        -- md5(string_agg id:inhoud_plain ORDER BY id) over embeddable elementen
    refreshed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    n_chunks      INT,
    PRIMARY KEY (scope_key, source_type)
);
