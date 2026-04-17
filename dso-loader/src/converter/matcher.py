"""Keyword-matcher: match bestemmingsplan-artikelen tegen bestaande
activiteitnamen en werkzaamheden uit OCD.

Geen LLM nodig — puur string-matching tegen het vocabulaire van 2.800+
activiteiten en 291 werkzaamheden die al in de database staan.
"""

import re
from dataclasses import dataclass, field

import psycopg
from rich.console import Console

from src.db import get_conn

console = Console()


@dataclass
class Match:
    naam: str
    bron: str           # 'activiteit' | 'werkzaamheid'
    identificatie: str   # OCD-identificatie van de match
    score: float         # 0-1: kwaliteit van de match
    context: str = ""    # stuk tekst waar de match in zit


def _normalize(text: str) -> str:
    """Lowercase, strip HTML, normaliseer whitespace."""
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.lower()
    text = re.sub(r"[^a-z0-9àáâãäåèéêëìíîïòóôõöùúûüÿ\s-]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _tokenize(text: str) -> set[str]:
    """Split in woorden van 4+ chars (filtert functiewoorden)."""
    return {w for w in text.split() if len(w) >= 4}


class ActivityMatcher:
    """Matcht artikeltekst tegen bestaande activiteiten en werkzaamheden."""

    def __init__(self, conn: psycopg.Connection | None = None):
        own_conn = conn is None
        if own_conn:
            conn = get_conn()
        try:
            self._load_vocab(conn)
        finally:
            if own_conn:
                conn.close()

    def _load_vocab(self, conn: psycopg.Connection):
        """Laad alle unieke activiteitnamen en werkzaamheden."""
        with conn.cursor() as cur:
            # Activiteiten: neem de meest voorkomende variant per naam
            cur.execute("""
                SELECT DISTINCT ON (lower(naam)) naam, identificatie
                FROM p2p.activiteit
                WHERE LENGTH(naam) BETWEEN 5 AND 120
                ORDER BY lower(naam), identificatie
            """)
            self.activiteiten = {
                _normalize(r["naam"]): (r["naam"], r["identificatie"])
                for r in cur.fetchall()
            }

            # Werkzaamheden
            cur.execute("SELECT naam, urn FROM i2a.werkzaamheid")
            self.werkzaamheden = {
                _normalize(r["naam"]): (r["naam"], r["urn"])
                for r in cur.fetchall()
            }

        console.print(f"  [dim]Matcher geladen: {len(self.activiteiten)} activiteiten, "
                      f"{len(self.werkzaamheden)} werkzaamheden[/dim]")

        # Bouw token-index voor snelle matching
        self._act_tokens: dict[str, list[str]] = {}
        for norm_naam in self.activiteiten:
            tokens = _tokenize(norm_naam)
            for token in tokens:
                self._act_tokens.setdefault(token, []).append(norm_naam)

    def match_artikel(self, tekst: str, min_score: float = 0.4) -> list[Match]:
        """Match een artikeltekst tegen het vocabulaire.

        Scoring:
        - Exacte substring-match van activiteitnaam in tekst: score 1.0
        - Meerdere tokens van activiteitnaam in tekst: score = overlap/totaal
        - Werkzaamheid-naam in tekst: score 0.9

        Returns matches gesorteerd op score (hoogste eerst).
        """
        norm_tekst = _normalize(tekst)
        tekst_tokens = _tokenize(norm_tekst)
        matches: list[Match] = []
        seen_namen: set[str] = set()

        # 1. Exacte substring-match op werkzaamheden (kort, specifiek)
        for wz_norm, (wz_naam, wz_urn) in self.werkzaamheden.items():
            if wz_norm in norm_tekst and wz_naam not in seen_namen:
                # Zoek context
                idx = norm_tekst.find(wz_norm)
                start = max(0, idx - 30)
                end = min(len(norm_tekst), idx + len(wz_norm) + 30)
                matches.append(Match(
                    naam=wz_naam,
                    bron="werkzaamheid",
                    identificatie=wz_urn,
                    score=0.9,
                    context=norm_tekst[start:end],
                ))
                seen_namen.add(wz_naam)

        # 2. Exacte substring-match op activiteiten (langer, minder kans)
        for act_norm, (act_naam, act_id) in self.activiteiten.items():
            if len(act_norm) >= 10 and act_norm in norm_tekst and act_naam not in seen_namen:
                idx = norm_tekst.find(act_norm)
                start = max(0, idx - 30)
                end = min(len(norm_tekst), idx + len(act_norm) + 30)
                matches.append(Match(
                    naam=act_naam,
                    bron="activiteit",
                    identificatie=act_id,
                    score=1.0,
                    context=norm_tekst[start:end],
                ))
                seen_namen.add(act_naam)

        # 3. Token-overlap matching (fuzzy)
        if tekst_tokens:
            candidate_scores: dict[str, float] = {}
            for token in tekst_tokens:
                for act_norm in self._act_tokens.get(token, []):
                    act_tokens = _tokenize(act_norm)
                    overlap = tekst_tokens & act_tokens
                    score = len(overlap) / len(act_tokens)
                    if score > candidate_scores.get(act_norm, 0):
                        candidate_scores[act_norm] = score

            for act_norm, score in candidate_scores.items():
                if score >= min_score:
                    act_naam, act_id = self.activiteiten[act_norm]
                    if act_naam not in seen_namen:
                        matches.append(Match(
                            naam=act_naam,
                            bron="activiteit-fuzzy",
                            identificatie=act_id,
                            score=round(score, 2),
                        ))
                        seen_namen.add(act_naam)

        matches.sort(key=lambda m: m.score, reverse=True)
        return matches


def _uid(prefix: str, bronhouder: str) -> str:
    import uuid
    return f"nl.imow-gm{bronhouder}.{prefix}.conv-{uuid.uuid4().hex[:12]}"


def persist_matches(conn: psycopg.Connection, regeling_expression: str,
                    artikel: dict, matches: list[Match],
                    bronhouder: str) -> int:
    """Sla keyword-matches op in conv.activiteit + conv.juridische_regel + ALA."""
    count = 0
    with conn.cursor() as cur:
        for m in matches:
            act_id = _uid("activiteit", bronhouder)

            cur.execute("""
                INSERT INTO conv.activiteit (identificatie, naam, bron)
                VALUES (%s, %s, 'keyword-match')
                ON CONFLICT DO NOTHING
            """, (act_id, m.naam))

            # Juridische regel koppelen aan het artikel
            jr_id = _uid("juridischeregel", bronhouder)
            cur.execute("""
                INSERT INTO conv.juridische_regel
                    (identificatie, regel_type, regeltekst_wid, bron)
                VALUES (%s, 'anders_geduid', %s, 'keyword-match')
                ON CONFLICT DO NOTHING
            """, (jr_id, artikel["wid"]))

            # ALA: koppel aan eerste beschikbare locatiegroep
            cur.execute("""
                SELECT l.identificatie FROM conv.locatie l
                WHERE l.identificatie LIKE %s
                  AND l.locatie_type = 'Gebiedengroep'
                LIMIT 1
            """, (f"%gm{bronhouder}%",))
            loc = cur.fetchone()
            if loc:
                cur.execute("""
                    INSERT INTO conv.activiteit_locatieaanduiding
                        (juridische_regel_id, activiteit_id, locatie_id, kwalificatie)
                    VALUES (%s, %s, %s, 'anders_geduid')
                """, (jr_id, act_id, loc["identificatie"]))

            count += 1
    return count


def match_bestemmingsplan(regeling_expression: str,
                          min_score: float = 0.5,
                          persist: bool = False,
                          persist_min_score: float = 0.7) -> dict:
    """Match alle artikelen van een geconverteerd plan tegen OCD-vocabulaire.

    persist=True slaat matches >= persist_min_score op in conv.*
    """
    conn = get_conn()
    try:
        matcher = ActivityMatcher(conn)

        # Haal bronhouder op
        with conn.cursor() as cur:
            cur.execute("SELECT bronhouder FROM conv.regeling WHERE frbr_expression = %s",
                        (regeling_expression,))
            reg = cur.fetchone()
            if not reg:
                raise ValueError(f"Regeling niet gevonden: {regeling_expression}")
            bronhouder = reg["bronhouder"]

            # Haal inhoudelijke artikelen
            cur.execute("""
                SELECT te.id, te.eid, te.wid, te.nummer, te.opschrift, te.inhoud
                FROM conv.tekst_element te
                LEFT JOIN conv.tekst_element parent ON parent.id = te.parent_id
                WHERE te.regeling_expression = %s
                  AND te.element_type = 'Artikel'
                  AND te.inhoud IS NOT NULL
                  AND LENGTH(te.inhoud) > 20
                  AND (parent.opschrift IS NULL OR parent.opschrift != 'Begrippen')
                ORDER BY te.volgorde
            """, (regeling_expression,))
            artikelen = cur.fetchall()

        console.print(f"  {len(artikelen)} inhoudelijke artikelen")
        all_matches: dict[str, list[Match]] = {}
        total = 0
        persisted = 0

        for art in artikelen:
            tekst = art["inhoud"] or ""
            matches = matcher.match_artikel(tekst, min_score=min_score)
            if matches:
                key = f"Art. {art['nummer']} {art['opschrift'] or ''}"
                all_matches[key] = matches
                total += len(matches)
                console.print(f"  Art. {art['nummer']}: {len(matches)} matches")
                for m in matches[:5]:
                    marker = " *" if persist and m.score >= persist_min_score else ""
                    console.print(f"    [{m.score:.0%}] {m.naam} ({m.bron}){marker}")

                if persist:
                    strong = [m for m in matches if m.score >= persist_min_score]
                    if strong:
                        n = persist_matches(conn, regeling_expression, art, strong, bronhouder)
                        persisted += n

        if persist:
            conn.commit()
            console.print(f"\n  [green]Totaal: {total} matches, {persisted} gepersisteerd "
                          f"(>= {persist_min_score:.0%}) in conv.activiteit[/green]")
        else:
            console.print(f"\n  [green]Totaal: {total} matches over {len(all_matches)} artikelen[/green]")
            console.print(f"  [dim]Gebruik --persist om matches >= 70% op te slaan[/dim]")

        return all_matches

    finally:
        conn.close()
