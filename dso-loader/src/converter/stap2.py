"""Stap 2: LLM-ondersteunde annotatie-voorstellen.

Gebruikt Groq (llama-3.3-70b) om per artikel activiteiten, normen en
juridische regels voor te stellen. Alles komt in conv.* met
bron='llm-voorstel'.
"""

import json
import os
import re
import uuid

import httpx
from rich.console import Console

from src.db import get_conn

console = Console()

# ── LLM client (Ollama of Groq) ─────────────────────────────────────


def _llm(system: str, user: str, temperature: float = 0.1) -> str:
    """Eén LLM-call via Ollama (lokaal) of Groq (cloud).

    Selectie via LLM_PROVIDER env var: 'ollama' (default) of 'groq'.
    """
    provider = os.getenv("LLM_PROVIDER", "ollama")

    if provider == "groq":
        return _llm_groq(system, user, temperature)
    else:
        return _llm_ollama(system, user, temperature)


def _llm_ollama(system: str, user: str, temperature: float) -> str:
    """LLM-call via lokale Ollama."""
    model = os.getenv("OLLAMA_MODEL", "llama3.1:8b")
    base = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")

    resp = httpx.post(
        f"{base}/api/chat",
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "options": {"temperature": temperature},
        },
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json().get("message", {}).get("content", "")


def _llm_groq(system: str, user: str, temperature: float) -> str:
    """LLM-call via Groq cloud API."""
    from groq import Groq

    api_key = os.getenv("GROQ_API_KEY", "")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY niet gezet in .env")

    client = Groq(api_key=api_key)
    model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=temperature,
        max_tokens=2000,
    )
    return resp.choices[0].message.content or ""


def _parse_json_response(text: str) -> dict | list | None:
    """Extraheer JSON uit LLM-response (soms gewrapt in ```json blocks)."""
    # Probeer direct
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Zoek ```json ... ``` block
    m = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # Zoek eerste { ... } of [ ... ]
    for start, end in [('{', '}'), ('[', ']')]:
        i = text.find(start)
        j = text.rfind(end)
        if i >= 0 and j > i:
            try:
                return json.loads(text[i:j + 1])
            except json.JSONDecodeError:
                pass
    return None


def _uid(prefix: str, bronhouder: str) -> str:
    return f"nl.imow-gm{bronhouder}.{prefix}.conv-{uuid.uuid4().hex[:12]}"


def _strip_html(html: str | None) -> str:
    """Strip HTML tags voor LLM-input."""
    if not html:
        return ""
    return re.sub(r"<[^>]+>", "", html).strip()


# ── Prompts ──────────────────────────────────────────────────────────

SYSTEM_ACTIVITEITEN = """Je bent een juridisch analist gespecialiseerd in de Nederlandse Omgevingswet.
Je analyseert bestemmingsplanartikelen en stelt IMOW-activiteiten voor.

Regels:
- Activiteitnamen in Ow-stijl: lowercase, zelfstandig naamwoord + werkwoord (bv. "bouwwerk bouwen", "boom kappen")
- Kwalificatie: een van: vergunningplichtig, meldingsplichtig, verbod, toegestaan, anders_geduid
- Als het artikel alleen begrippen of definities bevat: geef een lege lijst
- Als het artikel geen duidelijke activiteit beschrijft: geef een lege lijst
- Antwoord ALLEEN met valide JSON, geen toelichting"""

PROMPT_ACTIVITEITEN = """Analyseer dit bestemmingsplanartikel en stel IMOW-activiteiten voor.

ARTIKEL {nummer} - {opschrift}:
---
{tekst}
---

BESTEMMING: {bestemming}

Geef je antwoord als JSON array:
[
  {{
    "naam": "activiteitnaam in lowercase",
    "kwalificatie": "vergunningplichtig|meldingsplichtig|verbod|toegestaan|anders_geduid",
    "toelichting": "korte uitleg waarom"
  }}
]

Als er geen activiteiten uit dit artikel volgen, geef een lege array: []"""

SYSTEM_NORMEN = """Je bent een juridisch analist gespecialiseerd in de Nederlandse Omgevingswet.
Je extraheert kwantitatieve normen (bouwhoogte, oppervlakte, afstanden, etc.) uit bestemmingsplanartikelen.

Regels:
- Alleen concrete numerieke waarden of meetbare eisen
- Normnamen in Ow-stijl: "maximale bouwhoogte", "minimaal bebouwingspercentage", etc.
- Type: maximum, minimum, exact
- Eenheid: m, m2, %, stuks, dB, etc.
- Antwoord ALLEEN met valide JSON, geen toelichting"""

PROMPT_NORMEN = """Extraheer alle kwantitatieve normen uit dit bestemmingsplanartikel.

ARTIKEL {nummer} - {opschrift}:
---
{tekst}
---

Geef je antwoord als JSON array:
[
  {{
    "naam": "normnaam in lowercase",
    "type_norm": "maximum|minimum|exact",
    "eenheid": "m|m2|%|stuks|dB|...",
    "waarde": 10.0,
    "toelichting": "korte uitleg"
  }}
]

Als er geen normen in dit artikel staan, geef een lege array: []"""


# ── Extractie per artikel ────────────────────────────────────────────

def extract_activiteiten(artikel: dict, bestemming: str) -> list[dict]:
    """Extraheer activiteiten uit een artikel via LLM."""
    tekst = _strip_html(artikel.get("inhoud", ""))
    if len(tekst) < 15:
        return []

    prompt = PROMPT_ACTIVITEITEN.format(
        nummer=artikel.get("nummer", "?"),
        opschrift=artikel.get("opschrift", ""),
        tekst=tekst,
        bestemming=bestemming,
    )

    try:
        response = _llm(SYSTEM_ACTIVITEITEN, prompt)
        result = _parse_json_response(response)
        if isinstance(result, list):
            return result
    except Exception as e:
        console.print(f"      [red]LLM fout: {e}[/red]")
    return []


def extract_normen(artikel: dict) -> list[dict]:
    """Extraheer normen uit een artikel via LLM."""
    tekst = _strip_html(artikel.get("inhoud", ""))
    if len(tekst) < 15:
        return []

    prompt = PROMPT_NORMEN.format(
        nummer=artikel.get("nummer", "?"),
        opschrift=artikel.get("opschrift", ""),
        tekst=tekst,
    )

    try:
        response = _llm(SYSTEM_NORMEN, prompt)
        result = _parse_json_response(response)
        if isinstance(result, list):
            return result
    except Exception as e:
        console.print(f"      [red]LLM fout: {e}[/red]")
    return []


# ── Opslaan in conv-schema ───────────────────────────────────────────

def store_activiteiten(conn, regeling_expr: str, artikel: dict,
                       activiteiten: list[dict], bronhouder: str) -> int:
    """Sla voorgestelde activiteiten op in conv.*."""
    count = 0
    with conn.cursor() as cur:
        for act in activiteiten:
            naam = act.get("naam", "").strip()
            if not naam:
                continue
            kwalificatie = act.get("kwalificatie", "anders_geduid")
            act_id = _uid("activiteit", bronhouder)

            cur.execute("""
                INSERT INTO conv.activiteit (identificatie, naam, bron)
                VALUES (%s, %s, 'llm-voorstel')
                ON CONFLICT DO NOTHING
            """, (act_id, naam))

            # Juridische regel
            jr_id = _uid("juridischeregel", bronhouder)
            cur.execute("""
                INSERT INTO conv.juridische_regel
                    (identificatie, regel_type, regeltekst_wid, bron)
                VALUES (%s, %s, %s, 'llm-voorstel')
                ON CONFLICT DO NOTHING
            """, (jr_id, kwalificatie, artikel["wid"]))

            # ALA: koppel activiteit + juridische regel + locatie
            # Gebruik de eerste beschikbare locatie van deze regeling
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
                    VALUES (%s, %s, %s, %s)
                """, (jr_id, act_id, loc["identificatie"], kwalificatie))

            count += 1
    return count


def store_normen(conn, regeling_expr: str, artikel: dict,
                 normen: list[dict], bronhouder: str) -> int:
    """Sla geëxtraheerde normen op in conv.*."""
    count = 0
    with conn.cursor() as cur:
        for norm in normen:
            naam = norm.get("naam", "").strip()
            if not naam:
                continue
            norm_id = _uid("norm", bronhouder)
            type_norm = norm.get("type_norm", "maximum")
            eenheid = norm.get("eenheid")

            cur.execute("""
                INSERT INTO conv.norm
                    (identificatie, norm_type, naam, type_norm, eenheid, bron)
                VALUES (%s, 'omgevingsnorm', %s, %s, %s, 'llm-voorstel')
                ON CONFLICT DO NOTHING
            """, (norm_id, naam, type_norm, eenheid))

            # Normwaarde — onderscheid kwantitatief vs kwalitatief
            waarde = norm.get("waarde")
            if waarde is not None:
                cur.execute("""
                    SELECT l.identificatie FROM conv.locatie l
                    WHERE l.identificatie LIKE %s
                      AND l.locatie_type = 'Gebiedengroep'
                    LIMIT 1
                """, (f"%gm{bronhouder}%",))
                loc = cur.fetchone()
                if loc:
                    # Probeer als getal, anders als kwalitatieve waarde
                    try:
                        num_waarde = float(waarde)
                        cur.execute("""
                            INSERT INTO conv.normwaarde
                                (norm_id, locatie_id, kwantitatieve_waarde)
                            VALUES (%s, %s, %s)
                        """, (norm_id, loc["identificatie"], num_waarde))
                    except (ValueError, TypeError):
                        cur.execute("""
                            INSERT INTO conv.normwaarde
                                (norm_id, locatie_id, kwalitatieve_waarde)
                            VALUES (%s, %s, %s)
                        """, (norm_id, loc["identificatie"], str(waarde)))

            # Koppel aan juridische regel (als die er is voor dit artikel)
            cur.execute("""
                SELECT identificatie FROM conv.juridische_regel
                WHERE regeltekst_wid = %s LIMIT 1
            """, (artikel["wid"],))
            jr = cur.fetchone()
            if jr:
                cur.execute("""
                    INSERT INTO conv.juridische_regel_norm
                        (juridische_regel_id, norm_id)
                    VALUES (%s, %s)
                    ON CONFLICT DO NOTHING
                """, (jr["identificatie"], norm_id))

            count += 1
    return count


# ── Tophaak ──────────────────────────────────────────────────────────

def create_tophaak(conn, regeling_expr: str, bronhouder: str,
                   gemeente_naam: str, plan_naam: str) -> str:
    """Maak de tophaak-activiteit aan."""
    tophaak_id = _uid("activiteit", bronhouder)
    tophaak_naam = f"Activiteit gereguleerd in het omgevingsplan van gemeente {gemeente_naam}, deel {plan_naam}"

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO conv.activiteit
                (identificatie, naam, is_tophaak, bron)
            VALUES (%s, %s, TRUE, 'mechanisch')
            ON CONFLICT DO NOTHING
        """, (tophaak_id, tophaak_naam))

        # Alle bestaande activiteiten onder de tophaak hangen
        cur.execute("""
            UPDATE conv.activiteit
            SET bovenliggende = %s
            WHERE bron = 'llm-voorstel'
              AND bovenliggende IS NULL
              AND identificatie != %s
              AND identificatie LIKE %s
        """, (tophaak_id, tophaak_id, f"%gm{bronhouder}%"))

    return tophaak_id


# ── Bruidsschat-conflictdetectie ─────────────────────────────────────

def detect_bruidsschat_conflicts(conn, bronhouder: str) -> list[dict]:
    """Vergelijk conv-artikelen met bestaande p2p-omgevingsplan artikelen."""
    conflicts = []
    with conn.cursor() as cur:
        # Zoek omgevingsplan-artikelen van deze gemeente
        cur.execute("""
            SELECT te.opschrift, LEFT(te.inhoud, 500) AS inhoud, jr.thema
            FROM p2p.tekst_element te
            JOIN p2p.regeling r ON r.frbr_expression = te.regeling_expression
            LEFT JOIN p2p.juridische_regel jr ON jr.regeltekst_wid = te.wid
            WHERE r.bronhouder = %s
              AND r.documenttype = 'Omgevingsplan'
              AND te.element_type = 'Artikel'
              AND te.inhoud IS NOT NULL
            LIMIT 100
        """, (bronhouder,))
        ow_artikelen = cur.fetchall()

        if not ow_artikelen:
            return []

        # Zoek conv-artikelen
        cur.execute("""
            SELECT te.opschrift, LEFT(te.inhoud, 500) AS inhoud
            FROM conv.tekst_element te
            JOIN conv.regeling r ON r.frbr_expression = te.regeling_expression
            WHERE r.bronhouder = %s
              AND te.element_type = 'Artikel'
              AND te.inhoud IS NOT NULL
        """, (bronhouder,))
        conv_artikelen = cur.fetchall()

        if not conv_artikelen:
            return []

        # Simpele keyword-overlap detectie (zonder LLM — snel)
        for conv_art in conv_artikelen:
            conv_words = set(re.findall(r"\w{4,}", (conv_art["inhoud"] or "").lower()))
            if len(conv_words) < 3:
                continue

            for ow_art in ow_artikelen:
                ow_words = set(re.findall(r"\w{4,}", (ow_art["inhoud"] or "").lower()))
                overlap = conv_words & ow_words
                if len(overlap) > max(5, len(conv_words) * 0.3):
                    conflicts.append({
                        "conv_artikel": conv_art["opschrift"],
                        "ow_artikel": ow_art["opschrift"],
                        "overlap_woorden": len(overlap),
                        "type": "mogelijke overlap",
                    })

    return conflicts


# ── Orchestrator ─────────────────────────────────────────────────────

def annotate_bestemmingsplan(regeling_expression: str) -> dict:
    """Voer stap 2 uit voor een eerder geconverteerd bestemmingsplan."""
    from dotenv import load_dotenv
    load_dotenv()

    conn = get_conn()
    try:
        # Haal regeling-info op
        with conn.cursor() as cur:
            cur.execute("""
                SELECT r.frbr_expression, r.opschrift, r.bronhouder, b.naam AS gemeente_naam
                FROM conv.regeling r
                JOIN core.bronhouder b ON b.overheidscode = r.bronhouder
                WHERE r.frbr_expression = %s
            """, (regeling_expression,))
            reg = cur.fetchone()
            if not reg:
                raise ValueError(f"Regeling niet gevonden in conv: {regeling_expression}")

        bronhouder = reg["bronhouder"]
        plan_naam = reg["opschrift"]
        console.rule(f"[bold]Stap 2: {plan_naam}[/bold]")

        # Haal alle inhoudelijke artikelen (geen begrippen)
        with conn.cursor() as cur:
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

        console.print(f"  {len(artikelen)} inhoudelijke artikelen gevonden")

        # Bepaal de bestemming (meest voorkomende gebiedsaanwijzing)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT naam FROM conv.gebiedsaanwijzing
                WHERE identificatie LIKE %s
                ORDER BY naam LIMIT 1
            """, (f"%gm{bronhouder}%",))
            ga = cur.fetchone()
        bestemming = ga["naam"] if ga else "onbekend"

        total_act = 0
        total_norm = 0

        for i, art in enumerate(artikelen, 1):
            console.print(f"  [{i}/{len(artikelen)}] Art. {art['nummer']} {(art['opschrift'] or '')[:50]}")

            # Activiteiten
            acts = extract_activiteiten(art, bestemming)
            if acts:
                n = store_activiteiten(conn, regeling_expression, art, acts, bronhouder)
                total_act += n
                for a in acts:
                    console.print(f"    + activiteit: {a.get('naam', '?')} ({a.get('kwalificatie', '?')})")

            # Normen
            norms = extract_normen(art)
            if norms:
                n = store_normen(conn, regeling_expression, art, norms, bronhouder)
                total_norm += n
                for nm in norms:
                    console.print(f"    + norm: {nm.get('naam', '?')} = {nm.get('waarde', '?')} {nm.get('eenheid', '')}")

        # Tophaak
        console.print("  Tophaak aanmaken...")
        create_tophaak(conn, regeling_expression, bronhouder,
                       reg["gemeente_naam"], plan_naam)

        # Bruidsschat-conflicten
        console.print("  Bruidsschat-conflicten detecteren...")
        conflicts = detect_bruidsschat_conflicts(conn, bronhouder)
        if conflicts:
            console.print(f"  [yellow]{len(conflicts)} mogelijke overlap(s) met omgevingsplan[/yellow]")
            for c in conflicts[:5]:
                console.print(f"    {c['conv_artikel']} <-> {c['ow_artikel']} ({c['overlap_woorden']} woorden)")

        # Meta updaten
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE conv.conversie_meta
                SET stap = 2, bron = 'llm-voorstel',
                    llm_model = %s
                WHERE regeling_expression = %s
            """, (os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"),
                  regeling_expression))

        conn.commit()

        stats = {
            "regeling": regeling_expression,
            "artikelen": len(artikelen),
            "activiteiten": total_act,
            "normen": total_norm,
            "bruidsschat_conflicten": len(conflicts),
        }
        console.print(f"\n  [green]Stap 2 voltooid: {total_act} activiteiten, "
                      f"{total_norm} normen, {len(conflicts)} conflicten[/green]")
        return stats

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
