-- Vrijetekst-signalen in de data-health-laag
--
-- De health-laag mat tot nu toe alleen artikelstructuur (`artikel_dekking_pct`).
-- Voor omgevingsvisies en programma's was er geen enkel signaal, en juist daar
-- liet de loader velden vallen zonder dat iemand het zag: 2.934 tekstdelen met
-- een lege divisieverwijzing en 29.084 zonder idealisatie.
--
-- Deze view maakt drie dingen zichtbaar die na elke sync moeten kloppen:
--
--   vt_zonder_divisieref  moet 0 zijn. Meer dan 0 = de loader leest `divisieRef`
--                         niet meer, of de bron levert iets nieuws.
--   vt_zonder_idealisatie moet 0 zijn voor herladen voorraad.
--   vt_zonder_wid_brug    regelingen zonder rijen in p2p.divisie: dan is de
--                         koppeling annotatie -> tekst niet te leggen.
--
-- Zie docs/vrijetekst-gaten-plan.md. Idempotent.

CREATE OR REPLACE VIEW core.v_vrijetekst_health AS
WITH actief AS (
    SELECT frbr_expression, bronhouder
    FROM p2p.regeling
    WHERE regelingmodel = 'RegelingVrijetekst' AND NOT inactief
),
td AS (
    SELECT count(*)                                                      AS tekstdelen,
           count(*) FILTER (WHERE coalesce(t.divisie_wid, '') = '')      AS zonder_divisieref,
           count(*) FILTER (WHERE t.idealisatie IS NULL)                 AS zonder_idealisatie,
           count(*) FILTER (WHERE t.divisie_soort IS NULL)               AS zonder_soort,
           count(*) FILTER (WHERE t.divisie_soort = 'divisie')           AS op_divisieniveau,
           count(*) FILTER (WHERE t.idealisatie = 'indicatief')          AS indicatief
    FROM p2p.tekstdeel t
    JOIN actief a ON a.frbr_expression = t.regeling_expression
)
SELECT
    (SELECT count(*) FROM actief)                                        AS regelingen,
    td.tekstdelen,
    td.zonder_divisieref,
    td.zonder_idealisatie,
    td.zonder_soort,
    -- Annoteren op alleen divisieniveau is toegestaan maar afgeraden: Regels op
    -- de kaart toont zo'n annotatie niet bij de onderliggende divisieteksten.
    td.op_divisieniveau,
    td.indicatief,
    (SELECT count(*) FROM p2p.divisie)                                   AS divisie_rijen,
    -- Alleen regelingen die annotaties hébben kunnen een brug missen. Een
    -- document zonder enig tekstdeel levert ook geen divisies op; dat is geen
    -- loaderfout maar een leeg document, en die worden apart geteld.
    (SELECT count(*) FROM actief a
      WHERE EXISTS (SELECT 1 FROM p2p.tekstdeel td
                     WHERE td.regeling_expression = a.frbr_expression)
        AND NOT EXISTS (SELECT 1 FROM p2p.divisie d
                         WHERE d.regeling_expression = a.frbr_expression)) AS zonder_wid_brug,
    (SELECT count(*) FROM actief a
      WHERE NOT EXISTS (SELECT 1 FROM p2p.tekstdeel td
                         WHERE td.regeling_expression = a.frbr_expression)) AS zonder_annotaties,
    -- Hoeveel van de wId-brug landt daadwerkelijk op een tekstelement? Een
    -- lager getal dan `divisie_rijen` betekent dat de OW- en STOP-kant uit de
    -- pas lopen (bijvoorbeeld na een expressiewissel).
    (SELECT count(*) FROM p2p.divisie d
      JOIN p2p.tekst_element te ON te.wid = d.wid
                              AND te.regeling_expression = d.regeling_expression) AS wid_brug_sluitend
FROM td;

COMMENT ON VIEW core.v_vrijetekst_health IS
    'Gezondheid van de vrijetekst-annotaties (omgevingsvisie, programma). '
    'zonder_divisieref, zonder_idealisatie en zonder_soort horen 0 te zijn na '
    'een volledige herlading; een stijging betekent dat de loader velden laat '
    'vallen. Zie docs/vrijetekst-gaten-plan.md.';
