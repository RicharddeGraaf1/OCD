-- Fase 1 (inventaris) + Fase 2-B (hoge-zekerheid "opgenomen?"-signaal).
--
-- SCOPE (verbreed 2026-08-03): ALLE instructieregels, niet alleen die op het omgevingsplan.
-- IMOW annoteert een Instructieregel op twee elkaar uitsluitende assen:
--   * instructieregel_instrument      — welk DOCUMENT iets moet regelen (20 waarden in de data)
--   * instructieregel_taakuitoefening — welke ADRESSAAT een taak moet uitoefenen (9 waarden)
-- De omgevingsplan-set blijft de default-weergave van de site (`is_op`), maar de rest
-- (waterschapsverordening, omgevingsverordening, programma, projectbesluit, legger,
-- peilbesluit, en de hele adressaat-as) zit nu in de inventaris.
--
-- Het "opgenomen?"-signaal (Fase 2-B hieronder) is en blijft OMGEVINGSPLAN-ONLY: het
-- doorzoekt omgevingsplan-teksten en telt per gemeente. Rijen buiten die scope krijgen
-- status 'Niet gemeten' — expliciet niet-gemeten, geen impliciete "onbepaalbaar".
--
-- Fase 2-B is bewust GEEN per-cel LLM-oordeel, maar een schaalbaar, verdedigbaar signaal,
-- MET relevantie-constraint (provinciale instructie telt alleen voor gemeenten ín die
-- provincie; Rijk-instructie geldt overal):
--   * ZELF-GERAPPORTEERD (hoge zekerheid): de gebiedsaanwijzing-naam komt voor in de
--     OP-sectie "Wijze waarop aan instructieregels uitvoering wordt gegeven" — de gemeente
--     documenteert dáár zelf hoe ze de instructie uitvoert.
--   * (lead) GENOEMD: een SPECIFIEKE naam (<=20 OP's) komt voor in gewone OP-tekst. Onbevestigd.
--   * INDICATIEF (irm.opgenomen_indicatief, gevuld door match/fase2_fill.py): vector-
--     retrieval + LLM-entailment-judge tegen HET meest-gelokaliseerde OP in de eigen
--     provincie (Rijk -> Amsterdam). ja/deels -> "Waarschijnlijk voldaan (indicatief)",
--     nee -> "Niet gevonden (indicatief)". Expliciet indicatief, geen nalevingsoordeel.
--   * ONBEPAALBAAR: de rest (eerlijk getoond; toont de meetbaarheidsgrens).
\set ON_ERROR_STOP on

WITH prov_map(code, prov) AS (VALUES
  ('pv20','Groningen'),('pv21','Fryslân'),('pv22','Drenthe'),('pv23','Overijssel'),
  ('pv24','Flevoland'),('pv25','Gelderland'),('pv26','Utrecht'),('pv27','Noord-Holland'),
  ('pv28','Zuid-Holland'),('pv29','Zeeland'),('pv30','Noord-Brabant'),('pv31','Limburg')
),
ir AS (
  SELECT jr.identificatie AS ir_id,
         r.bronhouder, r.citeertitel AS bron, r.documenttype AS bron_type,
         -- Korte weergavetitel. Het DSO levert voor de Rijks-AMvB's/MR's een
         -- citeertitel die gelijk is aan het volle opschrift ("Besluit van
         -- 3 juli 2018, houdende regels over de kwaliteit … (Besluit kwaliteit
         -- leefomgeving)"). De officiële aanhaaltitel — het enige waar iemand
         -- naar zoekt — staat daar als staart-haakje in. Alleen voor die twee
         -- documenttypen uitgepakt: bij een programma of omgevingsplan is een
         -- staart-haakje géén aanhaaltitel maar een toevoeging ("(incl.
         -- Stolpen)", "(batterijen)") en zou dit de titel juist slopen.
         CASE WHEN r.documenttype IN ('AMvB','Ministeriële Regeling')
                   AND r.citeertitel ~ '\([^()]{4,}\)\s*$'
              THEN substring(r.citeertitel from '\(([^()]{4,})\)\s*$')
              ELSE r.citeertitel END AS bron_kort,
         CASE WHEN r.documenttype IN ('AMvB','Ministeriële Regeling') THEN 'Rijk'
              WHEN r.documenttype = 'Omgevingsverordening' THEN 'Provincie'
              ELSE 'Overig' END AS bestuurslaag,
         pm.prov AS prov_naam,               -- NULL voor Rijk → geldt overal
         jr.instructieregel_instrument AS instrument,
         jr.instructieregel_taakuitoefening AS adressaat,
         ('Omgevingsplan' = ANY(COALESCE(jr.instructieregel_instrument,'{}'))) AS is_op,
         -- welke as is geannoteerd? (sluiten elkaar uit in de data: 0 rijen met beide)
         CASE WHEN cardinality(COALESCE(jr.instructieregel_instrument,'{}')) > 0
                THEN 'instrument' ELSE 'adressaat' END AS doel_as,
         jr.thema, jr.regeltekst_wid, jr.regeling_expression,
         -- regeltekst_wid wijst vaak naar een Lid; het echte artikelnummer/-opschrift
         -- zit dan op de parent (Artikel). Los altijd naar artikel-niveau op.
         CASE WHEN te.element_type = 'Lid' THEN pte.nummer    ELSE te.nummer    END AS artikel_nummer,
         CASE WHEN te.element_type = 'Lid' THEN pte.opschrift ELSE te.opschrift END AS artikel_titel,
         CASE WHEN te.element_type = 'Lid' THEN te.nummer END  AS lid_nummer,
         -- Bouwstenen voor de deeplink naar Regels op de kaart. De viewer
         -- adresseert op FRBR-*work* (niet -expression) plus een eId-fragment;
         -- zie de encoder in de viewer. Artikel-niveau, net als de bestaande
         -- deeplink in omgevingsbot: het lid-eId (…__para_3) is geen knoop in
         -- de documentboom van de DSO-viewer, het artikel wel.
         r.frbr_work,
         CASE WHEN te.element_type = 'Lid' THEN pte.eid ELSE te.eid END AS artikel_eid,
         -- artikel-sleutel voor de artikel-telling (Monitor Werking Ow telt artikelen;
         -- wij annoteren per Regeltekst = artikel OF lid — beide eenheden rapporteren)
         r.frbr_expression || '|' ||
           COALESCE(CASE WHEN te.element_type = 'Lid' THEN pte.eid ELSE te.eid END,
                    jr.regeltekst_wid) AS artikel_key,
         te.inhoud_plain                       AS tekst,
         -- STOP-XML alleen meesturen wanneer er echte structuur in zit
         -- (lijsten, CALS-tabellen, begrippenlijsten): de drawer rendert die
         -- dan gestructureerd i.p.v. de geplette inhoud_plain. Platte
         -- Al-only teksten hebben niets aan de XML (scheelt ~0,9 MB).
         CASE WHEN te.inhoud ~ '<(Lijst|table|Begrippenlijst)'
              THEN te.inhoud END               AS tekst_xml,
         -- Precomputed begrijpelijke variant (geen juridische status) via de
         -- functioneel-id-koppeling; hash-mechaniek blijft in OCD intern.
         eh.begrijpelijk                       AS hertaling
  FROM p2p.juridische_regel jr
  JOIN p2p.regeling r ON r.frbr_expression = jr.regeling_expression
  LEFT JOIN prov_map pm ON pm.code = r.bronhouder
  LEFT JOIN p2p.tekst_element te  ON te.wid = jr.regeltekst_wid AND te.regeling_expression = jr.regeling_expression
  LEFT JOIN p2p.tekst_element pte ON pte.id = te.parent_id
  LEFT JOIN v2a.element_hertaling eh ON eh.tekst_element_id = te.id
         AND eh.model = 'claude-sonnet-5' AND eh.prompt_versie = 'v1'
  WHERE jr.regel_type = 'Instructieregel'
),
-- De omgevingsplan-deelverzameling: alléén hierop draait het opgenomen-signaal
-- en het gemeente-perspectief (doelpopulatie = de 342 omgevingsplannen).
ir_op AS (SELECT * FROM ir WHERE is_op),
ir_ga AS (
  SELECT ir.ir_id, g.type, g.naam
  FROM ir
  JOIN p2p.juridische_regel_gebiedsaanwijzing jga ON jga.juridische_regel_id = ir.ir_id
  JOIN p2p.gebiedsaanwijzing g ON g.identificatie = jga.gebiedsaanwijzing_id
),
-- ── Fase 2-B: opgenomen-signaal via gebiedsaanwijzing-naam ──
ir_naam AS (
  SELECT DISTINCT g.ir_id, g.naam, i.prov_naam
  FROM ir_ga g JOIN ir_op i ON i.ir_id = g.ir_id
  WHERE length(g.naam) > 10
),
naam_hits AS (   -- per (naam, gemeente): komt voor? in zelf-rapportage-toelichting? provincie?
  SELECT n.naam, r.bronhouder AS gem, gg.naam AS gem_naam, gg.provincie AS gem_prov,
         bool_or(e.kop_pad ILIKE '%Wijze waarop aan instructieregels uitvoering wordt gegeven%') AS in_zelfrap
  FROM (SELECT DISTINCT naam FROM ir_naam) n
  JOIN v2a.tekst_embedding e ON e.fts @@ phraseto_tsquery('dutch', n.naam)
  JOIN p2p.regeling r ON r.frbr_expression = e.regeling_expression AND r.documenttype = 'Omgevingsplan'
  LEFT JOIN core.gemeentegrens gg ON gg.overheidscode = r.bronhouder
  GROUP BY n.naam, r.bronhouder, gg.naam, gg.provincie
),
naam_freq AS (
  SELECT naam, COUNT(*) AS freq FROM naam_hits GROUP BY naam
),
opgenomen AS (
  SELECT inm.ir_id,
         COUNT(DISTINCT nh.gem) FILTER (
           WHERE nh.in_zelfrap AND (inm.prov_naam IS NULL OR inm.prov_naam = nh.gem_prov)) AS zelfrap_n,
         array_agg(DISTINCT nh.gem_naam) FILTER (
           WHERE nh.in_zelfrap AND (inm.prov_naam IS NULL OR inm.prov_naam = nh.gem_prov)) AS zelfrap_gem,
         COUNT(DISTINCT nh.gem) FILTER (
           WHERE nf.freq <= 20 AND NOT nh.in_zelfrap
             AND (inm.prov_naam IS NULL OR inm.prov_naam = nh.gem_prov)) AS genoemd_n
  FROM ir_naam inm
  JOIN naam_hits nh ON nh.naam = inm.naam
  JOIN naam_freq nf ON nf.naam = inm.naam
  GROUP BY inm.ir_id
),
indicatief AS (
  SELECT oi.ir_id, oi.verdict, oi.artikelen, oi.onderbouwing,
         COALESCE(gg.naam, oi.gemeente) AS gem_naam
  FROM irm.opgenomen_indicatief oi
  LEFT JOIN core.gemeentegrens gg ON gg.overheidscode = oi.gemeente
),
-- ── Doorwerking BUITEN het omgevingsplan (2026-08-03) ──────────────────────
-- Zelfde principe, andere doelpopulatie: per (instructieregel x doeldocument) een
-- oordeel, waarbij het doeldocument een omgevingsverordening, waterschapsverordening,
-- programma of projectbesluit is. Relevantie loopt via het regelingsgebied van het
-- doeldocument (irm.doel_relevantie), niet via gemeentegrenzen.
-- Er is hier GEEN zelfrapportage- of lead-signaal: de OP-sectie "Wijze waarop aan
-- instructieregels uitvoering wordt gegeven" bestaat alleen in omgevingsplannen.
doel_oordeel AS (
  SELECT c.ir_id, c.doeltype, c.doel_expression, c.doel_bronhouder, c.max_sim,
         j.verdict, j.artikelen, j.onderbouwing,
         COALESCE(NULLIF(dr.citeertitel,''), dr.opschrift) AS doel_titel,
         COALESCE(bh.naam, c.doel_bronhouder)              AS doel_overheid
  FROM irm.doelcel c
  JOIN irm.judge_uniek j ON j.ir_id = c.ir_id AND j.bewijs_hash = c.bewijs_hash
  LEFT JOIN p2p.regeling dr ON dr.frbr_expression = c.doel_expression
  LEFT JOIN core.bronhouder bh ON bh.overheidscode = c.doel_bronhouder
),
doel_agg AS (
  SELECT rel.ir_id,
         -- een regel kan meerdere instrumenten aanwijzen; dan telt de unie
         string_agg(DISTINCT rel.doeltype, ', ')  AS doeltype,
         count(DISTINCT rel.doel_expression)     AS n_doel,          -- relevante doeldocumenten
         count(DISTINCT rel.doel_bronhouder)     AS n_doel_overheden,
         count(DISTINCT o.doel_expression) FILTER (WHERE o.verdict IN ('ja','deels'))     AS n_ja,
         count(DISTINCT o.doel_expression) FILTER (WHERE o.verdict = 'nee'
                                                     AND o.max_sim >= 0.83)              AS n_nee_sterk,
         round(max(o.max_sim)::numeric, 2)       AS beste_sim,
         (SELECT json_agg(json_build_object('overheid', x.doel_overheid, 'document', x.doel_titel,
                                            'doeltype', x.doeltype, 'verdict', x.verdict,
                                            'artikelen', x.artikelen, 'onderbouwing', x.onderbouwing)
                 ORDER BY x.doel_overheid)
          FROM doel_oordeel x
          WHERE x.ir_id = rel.ir_id AND x.verdict IN ('ja','deels'))                      AS doel_verwerkt_in
  FROM irm.doel_relevantie rel
  LEFT JOIN doel_oordeel o ON o.ir_id = rel.ir_id AND o.doel_expression = rel.doel_expression
  GROUP BY rel.ir_id
),
-- ── Per (gemeente, instructieregel): hardste signaal (zelfrap > lead > AI) ──
gem_ir_naam AS (
  SELECT nh.gem, inm.ir_id,
         bool_or(nh.in_zelfrap)                       AS is_zelfrap,
         bool_or(NOT nh.in_zelfrap AND nf.freq <= 20) AS is_lead
  FROM naam_hits nh
  JOIN ir_naam inm ON inm.naam = nh.naam
  JOIN naam_freq nf ON nf.naam = nh.naam
  WHERE inm.prov_naam IS NULL OR inm.prov_naam = nh.gem_prov
  GROUP BY nh.gem, inm.ir_id
),
gem_ir_ai AS (
  -- doel-gemeente-oordelen (fase2_fill) + volle-matrix-oordelen (tier1 screening ×
  -- tier2 judge per unieke bewijs-set; verdict werkt via bewijs_hash door naar alle
  -- gemeenten met dezelfde bewijs-teksten)
  SELECT gemeente AS gem, ir_id, true AS is_ai
  FROM irm.opgenomen_indicatief
  WHERE verdict IN ('ja','deels')
  UNION
  SELECT c.gem, c.ir_id, true
  FROM irm.screening_cel c
  JOIN irm.judge_uniek j ON j.ir_id = c.ir_id AND j.bewijs_hash = c.bewijs_hash
  WHERE j.verdict IN ('ja','deels')
),
-- Herstratificatie op OPGESLAGEN zekerheid (geen nieuwe judge-run): de zwakste
-- bevestigde 'ja' had max_sim 0,827 → nee-oordelen met beste bewijs < 0,83 zijn
-- "bewijs te zwak om iets te zeggen" (Onbepaalbaar), niet "Niet gevonden".
matrix_stats AS (
  SELECT c.ir_id,
         max(c.max_sim) AS beste_sim,
         COUNT(DISTINCT c.gem) FILTER (WHERE j.verdict IN ('ja','deels'))            AS n_gem_ja,
         COUNT(*) FILTER (WHERE j.verdict = 'nee' AND c.max_sim >= 0.83)             AS n_cel_nee_sterk,
         COUNT(*) FILTER (WHERE j.verdict = 'nee' AND c.max_sim <  0.83)             AS n_cel_nee_zwak
  FROM irm.screening_cel c
  JOIN irm.judge_uniek j ON j.ir_id = c.ir_id AND j.bewijs_hash = c.bewijs_hash
  GROUP BY c.ir_id
),
gem_ir AS (
  SELECT COALESCE(n.gem, a.gem)        AS gem,
         COALESCE(n.ir_id, a.ir_id)    AS ir_id,
         COALESCE(n.is_zelfrap, false) AS is_zelfrap,
         COALESCE(n.is_lead, false)    AS is_lead,
         COALESCE(a.is_ai, false)      AS is_ai
  FROM gem_ir_naam n
  FULL OUTER JOIN gem_ir_ai a ON a.gem = n.gem AND a.ir_id = n.ir_id
),
-- Per instructieregel: in welke plannen teruggevonden + hardste methode
ir_verwerkt_in AS (
  SELECT gi.ir_id,
         json_agg(json_build_object(
           'gemeente', gg.naam, 'provincie', gg.provincie,
           'methode', CASE WHEN gi.is_zelfrap THEN 'zelfrap'
                           WHEN gi.is_lead    THEN 'lead'
                           ELSE 'ai' END)
           ORDER BY (CASE WHEN gi.is_zelfrap THEN 0 WHEN gi.is_lead THEN 1 ELSE 2 END), gg.naam) AS verwerkt_in
  FROM gem_ir gi
  JOIN core.gemeentegrens gg ON gg.overheidscode = gi.gem
  WHERE gi.is_zelfrap OR gi.is_lead OR gi.is_ai
  GROUP BY gi.ir_id
),
rows AS (
  SELECT ir.ir_id AS id, ir.bron, ir.bron_kort, ir.bron_type, ir.bestuurslaag, ir.bronhouder,
         ir.instrument, ir.adressaat, ir.thema, ir.is_op, ir.doel_as,
         -- compacte artikel-sleutel: laat de viewer onder ELK filter exact het aantal
         -- artikelen tellen (meerdere leden van één artikel delen deze id)
         dense_rank() OVER (ORDER BY ir.artikel_key) AS art_id,
         ir.artikel_nummer, ir.artikel_titel, ir.lid_nummer, ir.tekst, ir.tekst_xml, ir.hertaling,
         ir.frbr_work, ir.artikel_eid,
         (SELECT json_agg(json_build_object('type', g.type, 'naam', g.naam))
          FROM ir_ga g WHERE g.ir_id = ir.ir_id) AS gebiedsaanwijzingen,
         CASE WHEN EXISTS (SELECT 1 FROM ir_ga g WHERE g.ir_id = ir.ir_id) THEN 'A' ELSE 'C' END AS klasse,
         COALESCE(o.zelfrap_n, 0)  AS zelfrap_n,
         o.zelfrap_gem             AS zelfrap_gemeenten,
         COALESCE(o.genoemd_n, 0)  AS genoemd_n,
         i.verdict      AS indicatief_verdict,
         i.gem_naam     AS indicatief_gemeente,
         i.artikelen    AS indicatief_artikelen,
         i.onderbouwing AS indicatief_onderbouwing,
         COALESCE(ms.n_gem_ja, 0) AS matrix_ja_n,
         round(ms.beste_sim::numeric, 2) AS bewijs_sterkte,
         COALESCE(ms.n_cel_nee_sterk, 0) AS n_cel_nee_sterk,
         COALESCE(ms.n_cel_nee_zwak, 0)  AS n_cel_nee_zwak,
         -- doorwerking buiten het omgevingsplan
         da.doeltype        AS doel_type,
         da.n_doel          AS doel_n,
         da.n_doel_overheden AS doel_n_overheden,
         COALESCE(da.n_ja, 0)         AS doel_ja_n,
         COALESCE(da.n_nee_sterk, 0)  AS doel_nee_sterk_n,
         da.beste_sim       AS doel_beste_sim,
         COALESCE(da.doel_verwerkt_in, '[]'::json) AS doel_verwerkt_in,
         CASE
           -- buiten het omgevingsplan: oordeel uit de doeldocument-matrix, mits die
           -- er is. Geen zelfrapportage/lead-signaal beschikbaar (OP-only).
           WHEN NOT ir.is_op AND da.ir_id IS NOT NULL THEN
             CASE WHEN COALESCE(da.n_ja,0) > 0 THEN 'Waarschijnlijk voldaan (indicatief)'
                  WHEN COALESCE(da.n_nee_sterk,0) > 0
                   AND COALESCE(da.beste_sim,0) >= 0.83 THEN 'Niet gevonden (indicatief)'
                  ELSE 'Onbepaalbaar' END
           -- geen doelpopulatie doorzocht: expliciet niet-gemeten i.p.v. een
           -- misleidend 'Onbepaalbaar'
           WHEN NOT ir.is_op THEN 'Niet gemeten'
           WHEN COALESCE(o.zelfrap_n,0) > 0 THEN 'Zelf-gerapporteerd'
           WHEN i.verdict IN ('ja','deels')
             OR COALESCE(ms.n_gem_ja,0) > 0 THEN 'Waarschijnlijk voldaan (indicatief)'
           -- alleen "Niet gevonden" claimen als er ergens STERK bewijs is beoordeeld
           WHEN (i.verdict = 'nee' OR COALESCE(ms.n_cel_nee_sterk,0) > 0)
             AND COALESCE(ms.beste_sim,0) >= 0.83 THEN 'Niet gevonden (indicatief)'
           ELSE 'Onbepaalbaar'
         END AS opgenomen_status,
         ir.prov_naam,
         COALESCE((SELECT vi.verwerkt_in FROM ir_verwerkt_in vi WHERE vi.ir_id = ir.ir_id), '[]'::json) AS verwerkt_in
  FROM ir
  LEFT JOIN opgenomen o ON o.ir_id = ir.ir_id
  LEFT JOIN indicatief i ON i.ir_id = ir.ir_id
  LEFT JOIN matrix_stats ms ON ms.ir_id = ir.ir_id
  LEFT JOIN doel_agg da ON da.ir_id = ir.ir_id
),
-- ── Gemeente-perspectief: totalen per bestuurslaag ──
rijk_tot AS (SELECT COUNT(*) AS n FROM ir_op WHERE prov_naam IS NULL),
prov_tot AS (SELECT prov_naam, COUNT(*) AS n FROM ir_op WHERE prov_naam IS NOT NULL GROUP BY prov_naam),
gem_verwerkt AS (
  SELECT gem,
         COUNT(*) FILTER (WHERE is_zelfrap)                               AS n_zelfrap,
         COUNT(*) FILTER (WHERE is_lead AND NOT is_zelfrap)               AS n_lead,
         COUNT(*) FILTER (WHERE is_ai AND NOT is_lead AND NOT is_zelfrap) AS n_ai
  FROM gem_ir GROUP BY gem
),
gemeenten AS (
  SELECT gg.overheidscode AS code, gg.naam, gg.provincie,
         (SELECT n FROM rijk_tot) + COALESCE(pt.n, 0) AS relevante_regels,
         COALESCE(gv.n_zelfrap,0) AS n_zelfrap,
         COALESCE(gv.n_lead,0)    AS n_lead,
         COALESCE(gv.n_ai,0)      AS n_ai,
         COALESCE(gv.n_zelfrap,0)+COALESCE(gv.n_lead,0)+COALESCE(gv.n_ai,0) AS verwerkt,
         round(100.0 * (COALESCE(gv.n_zelfrap,0)+COALESCE(gv.n_lead,0)+COALESCE(gv.n_ai,0))
               / NULLIF((SELECT n FROM rijk_tot) + COALESCE(pt.n, 0), 0), 1) AS verwerkt_pct
  FROM core.gemeentegrens gg
  LEFT JOIN prov_tot pt ON pt.prov_naam = gg.provincie
  LEFT JOIN gem_verwerkt gv ON gv.gem = gg.overheidscode
),
-- ── Doeloverheden: hetzelfde perspectief als `gemeenten`, maar voor de andere
--    doeltypen. Eén rij per (doeltype, overheid): hoeveel instructieregels op haar
--    van toepassing zijn en in hoeveel daarvan een uitwerking is teruggevonden.
--    Voor programma en projectbesluit kan één overheid meerdere documenten hebben;
--    dan telt "in ≥1 van haar documenten teruggevonden".
doeloverheden AS (
  SELECT rel.doeltype,
         COALESCE(bh.naam, r.bronhouder)             AS naam,
         r.bronhouder                                AS code,
         COUNT(DISTINCT rel.doel_expression)         AS documenten,
         COUNT(DISTINCT rel.ir_id)                   AS relevante_regels,
         COUNT(DISTINCT rel.ir_id) FILTER (WHERE o.verdict IN ('ja','deels')) AS verwerkt,
         round(100.0 * COUNT(DISTINCT rel.ir_id) FILTER (WHERE o.verdict IN ('ja','deels'))
               / NULLIF(COUNT(DISTINCT rel.ir_id),0), 1)                      AS verwerkt_pct,
         -- welke regels gelden voor deze overheid: nodig om in het detailpaneel óók
         -- de niet-teruggevonden regels te kunnen tonen. Alleen voor de doeltypen
         -- waar één overheid één document heeft; bij programma zou dit de JSON met
         -- ~1 MB aan id's opblazen zonder dat er een plicht-populatie tegenover staat.
         CASE WHEN rel.doeltype IN ('Omgevingsverordening','Waterschapsverordening')
              THEN array_agg(DISTINCT rel.ir_id) END                          AS regel_ids
  FROM irm.doel_relevantie rel
  JOIN p2p.regeling r  ON r.frbr_expression = rel.doel_expression
  LEFT JOIN core.bronhouder bh ON bh.overheidscode = r.bronhouder
  LEFT JOIN doel_oordeel o ON o.ir_id = rel.ir_id AND o.doel_expression = rel.doel_expression
  GROUP BY rel.doeltype, COALESCE(bh.naam, r.bronhouder), r.bronhouder
)
SELECT json_build_object(
  'gemeenten', (SELECT json_agg(row_to_json(gemeenten)
                ORDER BY verwerkt DESC, n_zelfrap DESC, naam) FROM gemeenten),
  'doeloverheden', (SELECT json_agg(row_to_json(doeloverheden)
                ORDER BY doeltype, verwerkt DESC, naam) FROM doeloverheden),
  'meta', json_build_object(
     'titel', 'Instructieregels — inventaris (alle doelen) + doorwerking (omgevingsplan, '
              'omgevingsverordening, waterschapsverordening, programma, projectbesluit)',
     'gegenereerd', :'gendate',
     -- n_instructieregels / n_artikelen blijven de OMGEVINGSPLAN-cijfers: dat is de
     -- default-weergave en het getal dat met de Monitor Werking Ow vergeleken wordt.
     'n_instructieregels', (SELECT COUNT(*) FROM rows WHERE is_op),
     'n_artikelen', (SELECT COUNT(DISTINCT artikel_key) FROM ir_op),
     'n_totaal', (SELECT COUNT(*) FROM rows),
     'n_artikelen_totaal', (SELECT COUNT(DISTINCT artikel_key) FROM ir),
     'artikelen_per_bestuurslaag', (SELECT json_object_agg(bestuurslaag, n) FROM
        (SELECT bestuurslaag, COUNT(DISTINCT artikel_key) n FROM ir_op GROUP BY bestuurslaag) s),
     'per_bestuurslaag', (SELECT json_object_agg(bestuurslaag, n) FROM (SELECT bestuurslaag, COUNT(*) n FROM rows WHERE is_op GROUP BY bestuurslaag) s),
     'per_bron', (SELECT json_agg(json_build_object('bron', bron, 'bestuurslaag', bestuurslaag, 'n', n) ORDER BY n DESC) FROM (SELECT bron, bestuurslaag, COUNT(*) n FROM rows GROUP BY bron, bestuurslaag) s),
     'per_opgenomen', (SELECT json_object_agg(opgenomen_status, n) FROM (SELECT opgenomen_status, COUNT(*) n FROM rows WHERE is_op GROUP BY opgenomen_status) s),
     'klasse_A', (SELECT COUNT(*) FROM rows WHERE is_op AND klasse='A'),
     'klasse_C', (SELECT COUNT(*) FROM rows WHERE is_op AND klasse='C'),
     -- verbrede assen (over de VOLLEDIGE set)
     'per_doel_as', (SELECT json_object_agg(doel_as, n) FROM (SELECT doel_as, COUNT(*) n FROM rows GROUP BY doel_as) s),
     'per_instrument', (SELECT json_object_agg(v, n) FROM
        (SELECT unnest(instrument) v, COUNT(*) n FROM rows GROUP BY 1) s),
     'per_adressaat', (SELECT json_object_agg(v, n) FROM
        (SELECT unnest(adressaat) v, COUNT(*) n FROM rows GROUP BY 1) s),
     'artikelen_per_instrument', (SELECT json_object_agg(v, n) FROM
        (SELECT unnest(instrument) v, COUNT(DISTINCT artikel_key) n FROM ir GROUP BY 1) s),
     'artikelen_per_adressaat', (SELECT json_object_agg(v, n) FROM
        (SELECT unnest(adressaat) v, COUNT(DISTINCT artikel_key) n FROM ir GROUP BY 1) s),
     -- doorwerking buiten het omgevingsplan: gemeten per doeltype
     'doel_gemeten', (SELECT json_object_agg(doel_type, json_build_object(
          'regels', n, 'documenten', docs, 'met_signaal', ja))
        FROM (SELECT doel_type, COUNT(*) n, max(doel_n) docs,
                     COUNT(*) FILTER (WHERE doel_ja_n > 0) ja
              FROM rows WHERE doel_type IS NOT NULL GROUP BY 1) s),
     -- volledigheid van de doeldocument-meting: cellen met een bewijs-set versus
     -- cellen die ook daadwerkelijk beoordeeld zijn (voorkomt dat een halve
     -- judge-run er als "niets gevonden" uitziet)
     'doel_judge_dekking', (SELECT json_object_agg(doeltype, json_build_object(
          'cellen', cellen, 'beoordeeld', beoordeeld))
        FROM (SELECT c.doeltype, COUNT(*) cellen,
                     COUNT(j.verdict) beoordeeld
              FROM irm.doelcel c
              LEFT JOIN irm.judge_uniek j ON j.ir_id=c.ir_id AND j.bewijs_hash=c.bewijs_hash
              GROUP BY 1) s)
  ),
  'instructieregels', (SELECT json_agg(row_to_json(rows) ORDER BY bestuurslaag, bron, artikel_nummer) FROM rows)
);
