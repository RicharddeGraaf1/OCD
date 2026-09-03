"""Backfill van de IMTR-velden die de parser weggooide (gaps#G-138).

Bewust in twee losse stappen, en dat is de belangrijkste ontwerpkeuze:

    1. `haal_op()`   — netwerk. Serieel, traag, herstartbaar. Zet het ruwe
                       sttrBestand gzipped in i2a.sttr_bestand.
    2. `parse()`     — lokaal. Nul API-calls. Leest uit i2a.sttr_bestand en
                       vult i2a.uitvoeringsregel.

Zonder die scheiding kost elke parser-fix opnieuw ~52.500 calls. Mét is het een
lokale herparse: zet `geparsed_op` op NULL en draai stap 2 opnieuw.

De ophaalstap gebruikt `src.beleefde_client.Beleefd` (1 req/s, één verbinding,
stopt bij herhaalde 503) en staat standaard op het voorkeursvenster
22:00-06:00 dat het stelsel zelf noemt.
"""

from __future__ import annotations

import gzip
import re

from lxml import etree
from rich.console import Console

from src.beleefde_client import Beleefd, DienstWijktAf
from src.config import cfg
from src.db import get_conn

console = Console()

PAGE = 500  # gemeten: de lijst accepteert echt 500 (gecapt op 500)

# GEEN vaste namespace-URI's. De IMTR-extensies bestaan in minstens twee
# versies naast elkaar: de Dordrecht-productiebestanden gebruiken
# .../v1.0/Uitvoeringsregel, de officiele STTR 3.0.0-set .../v2.0/... . In een
# steekproef van 200 live bestanden kwam sttrVersie 1 (185x), 2 (13x) en 3 (2x)
# voor. Een hardgecodeerde v1.0-namespace mist die bestanden stil — geen fout,
# gewoon nul regels. Daarom matchen we op local-name.

# De tien typen uit IMTR 3.0.1 §6.1, als kindelement van uitv:uitvoeringsregel.
# Volgorde is de detectievolgorde; het eerste dat voorkomt bepaalt het type.
# De spelling volgt de bestanden, niet de spec-proza: het is
# `vasteWaardeVoorbehoud` (niet ...OnderVoorbehoud) en `registerbevragingAPIProfiel`
# met hoofdletter-API. Beide vielen bij de eerste versie stil in de generieke bak.
TYPEN = (
    "geoVerwijzing",
    "uitkomstHerbruikbareBeslissing",
    "herbruikbareBeslissing",
    "registerBevraging",
    "registerbevragingAPIProfiel",
    "vasteWaardeVoorbehoud",
    "vasteWaarde",
    "implicietAntwoord",
    "bijlage",
    "vraag",
)
# Hoofdletterongevoelig, zodat een spellingvariant niet stil doorvalt.
_TYPE_MAP = {t.lower(): t for t in TYPEN}


# ---------------------------------------------------------------- stap 1
def _items(payload: dict) -> list[dict]:
    emb = payload.get("_embedded", {}) or {}
    return emb.get("toepasbareRegelsList") or emb.get("toepasbareRegels") or []


def haal_op(tempo: float = 1.0, alleen_s_nachts: bool = True,
            budget: int | None = None) -> dict:
    """Download ontbrekende sttrBestanden naar i2a.sttr_bestand.

    budget = harde bovengrens op het aantal bestanden deze run. De tabel is het
    checkpoint, dus een volgende run pakt de rest.
    """
    conn = get_conn()
    stats = {"nieuw": 0, "overgeslagen": 0, "bytes": 0, "calls": 0}
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT sttr_id FROM i2a.sttr_bestand")
            binnen = {r["sttr_id"] for r in cur.fetchall()}
        console.print(f"  {len(binnen)} bestanden al binnen")

        with Beleefd(tempo=tempo, alleen_s_nachts=alleen_s_nachts) as c:
            page = 1
            while True:
                r = c.get(f"{cfg.STTR_BASE}/toepasbareRegels",
                          params={"pageSize": PAGE, "page": page})
                r.raise_for_status()
                j = r.json()
                items = _items(j)
                if not items:
                    break

                for it in items:
                    href = (it.get("_links", {}) or {}).get("self", {}).get("href", "")
                    if "/toepasbareRegels/" not in href:
                        continue
                    sid = href.split("/toepasbareRegels/")[1].split("?")[0]
                    if sid in binnen:
                        stats["overgeslagen"] += 1
                        continue
                    if budget and stats["nieuw"] >= budget:
                        console.print(f"  [yellow]Budget van {budget} bereikt — "
                                      f"gestopt. Volgende run gaat verder.[/yellow]")
                        conn.commit()
                        stats["calls"] = c.calls
                        return stats

                    q = c.get(f"{cfg.STTR_BASE}/toepasbareRegels/{sid}/sttrBestand")
                    if q.status_code != 200:
                        continue
                    rauw = q.content
                    with conn.cursor() as cur:
                        cur.execute(
                            """INSERT INTO i2a.sttr_bestand
                                   (sttr_id, fsr, oin, sttr_versie,
                                    laatste_wijziging, bytes_rauw, xml_gz)
                               VALUES (%s,%s,%s,%s,%s,%s,%s)
                               ON CONFLICT (sttr_id) DO NOTHING""",
                            (sid, it.get("functioneleStructuurRef"), it.get("oin"),
                             it.get("sttrVersie"), it.get("laatsteWijzigingDatum"),
                             len(rauw), gzip.compress(rauw, 6)))
                    conn.commit()
                    binnen.add(sid)
                    stats["nieuw"] += 1
                    stats["bytes"] += len(rauw)
                    if stats["nieuw"] % 100 == 0:
                        console.print(f"    {stats['nieuw']} nieuw, "
                                      f"{stats['bytes'] / 1e6:.0f} MB rauw, "
                                      f"{c.calls} calls")

                if not (j.get("_links", {}) or {}).get("next"):
                    break
                page += 1
            stats["calls"] = c.calls
    except DienstWijktAf as e:
        conn.commit()
        console.print(f"  [yellow]Gestopt: {e}[/yellow]")
    finally:
        conn.close()
    console.print(f"  [green]{stats['nieuw']} nieuw · {stats['overgeslagen']} al binnen "
                  f"· {stats['calls']} calls[/green]")
    return stats


# ---------------------------------------------------------------- stap 2
def _lok(el) -> str:
    """Local-name van een element, namespace-onafhankelijk."""
    t = el.tag
    return t.rsplit("}", 1)[-1] if isinstance(t, str) else ""


def _kind(el, naam: str):
    """Eerste directe kind met deze local-name, ongeacht namespace."""
    for k in el:
        if _lok(k) == naam:
            return k
    return None


def _tekst(el, naam: str) -> str | None:
    k = _kind(el, naam)
    return (k.text or "").strip() if k is not None and k.text else None


# De bron escapet leestekens als markdown: "uitvoeren\." in plaats van
# "uitvoeren.". Ongedaan maken, anders staat die backslash straks op het scherm.
_ESCAPE = re.compile(r"\\([.\-+*_#()\[\]!>])")


def _schoon(t: str | None) -> str | None:
    if not t:
        return None
    return _ESCAPE.sub(r"\1", t).strip() or None


def _diep(el, *namen: str):
    """Afdalen langs local-names: _diep(u, 'vraag', 'vraagTekst')."""
    for naam in namen:
        if el is None:
            return None
        el = _kind(el, naam)
    return el


def _tekst_van(el) -> str | None:
    """Alle tekst onder een element, CDATA meegerekend."""
    return _schoon("".join(el.itertext())) if el is not None else None


def _ontleed(xml: bytes, sttr_id: str) -> list[dict]:
    """Alle uitvoeringsregels uit één bestand, met type en bruggen."""
    try:
        root = etree.fromstring(xml)
    except etree.XMLSyntaxError:
        return []

    # inputData -> uitvoeringsregelRef, zodat we de decision-kant kunnen koppelen.
    ref_naar_input: dict[str, str] = {}
    for inp in root.iter():
        if _lok(inp) != "inputData":
            continue
        for r in inp.iter():
            if _lok(r) == "uitvoeringsregelRef":
                href = (r.get("href") or "").lstrip("#")
                if href:
                    ref_naar_input[href] = inp.get("id", "")

    uit = []
    for u in root.iter():
        if _lok(u) != "uitvoeringsregel":
            continue
        uid = u.get("id") or ""
        soort, nen, act, gtype = "Uitvoeringsregel", None, None, None
        for k in u:
            t = _TYPE_MAP.get(_lok(k).lower())
            if t is None:
                continue
            soort = t[0].upper() + t[1:]
            if t == "geoVerwijzing":
                loc = _kind(k, "locatie")
                nen = loc.get("identificatie") if loc is not None else None
            elif t == "uitkomstHerbruikbareBeslissing":
                a = _kind(k, "activiteit")
                act = a.get("urn") if a is not None else None
            elif t == "vraag":
                gtype = _tekst(k, "gegevensType")
            break

        # De leesbare kant. Bij een Vraag is dat de vraagtekst, bij een Bijlage
        # het type document dat gevraagd wordt; beide zijn wat de gebruiker ziet.
        label = _tekst_van(_diep(u, "vraag", "vraagTekst"))             or _tekst_van(_diep(u, "bijlage", "bijlageType"))
        toel = _tekst_van(_diep(u, "uitvoeringsregelToelichting", "toelichting"))
        prio = _tekst(u, "prioriteit")

        opties, optie_type = None, None
        opt_el = _diep(u, "vraag", "opties")
        if opt_el is not None:
            optie_type = _tekst(opt_el, "optieType")
            paren = []
            for o in opt_el:
                if _lok(o) != "optie":
                    continue
                tekst = _tekst_van(_kind(o, "optieText"))
                if tekst:
                    volg = _tekst(o, "sequenceId")
                    paren.append((int(volg) if (volg or "").isdigit() else 999, tekst))
            opties = [t for _, t in sorted(paren)] or None
        uit.append({
            "sttr_id": sttr_id, "uitv_dmn_id": uid, "regel_type": soort,
            "bereik": _tekst(u, "bereik"), "gegevens_type": gtype,
            "nen3610_id": nen, "activiteit_urn": act,
            "input_dmn_id": ref_naar_input.get(uid),
            "label": label, "toelichting": toel, "opties": opties,
            "optie_type": optie_type,
            "prioriteit": int(prio) if (prio or "").isdigit() else None,
        })
    return uit


def parse(limit: int | None = None, opnieuw: bool = False) -> dict:
    """Lokale parse over i2a.sttr_bestand. Nul API-calls."""
    conn = get_conn()
    stats = {"bestanden": 0, "regels": 0, "geo": 0, "herbruik": 0, "bereik": 0,
             "label": 0, "opties": 0}
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT sttr_id, fsr, xml_gz FROM i2a.sttr_bestand "
                + ("" if opnieuw else "WHERE geparsed_op IS NULL ")
                + "ORDER BY sttr_id" + (" LIMIT %s" if limit else ""),
                (limit,) if limit else ())
            rijen = cur.fetchall()
        console.print(f"  {len(rijen)} bestanden te parsen")

        for i, rij in enumerate(rijen, 1):
            regels = _ontleed(gzip.decompress(rij["xml_gz"]), rij["sttr_id"])
            with conn.cursor() as cur:
                # Twee opruimacties, en de tweede is niet vanzelfsprekend.
                # (1) een eerdere parse van ditzelfde bestand;
                # (2) de rijen die de OUDE loader voor deze namespace schreef.
                #     Die dragen alleen een tweewaardig regel_type en verder
                #     niets, en zonder deze regel staan ze naast de nieuwe:
                #     gemeten 1.238.206 oude naast 118.754 nieuwe, voor 17.644
                #     namespaces dubbel. Wie regels telt, telt dan dubbel.
                # Veilig omdat een namespace vrijwel nooit meerdere bestanden
                # host (3 van 17.644) en geen daarvan half geparsed is; anders
                # zou een ongeparsed zusterbestand hier zijn rijen verliezen.
                cur.execute("DELETE FROM i2a.uitvoeringsregel WHERE sttr_id = %s",
                            (rij["sttr_id"],))
                cur.execute("DELETE FROM i2a.uitvoeringsregel "
                            " WHERE regelbestand_ns = %s AND sttr_id IS NULL",
                            (rij["fsr"],))
                for r in regels:
                    cur.execute(
                        """INSERT INTO i2a.uitvoeringsregel
                               (regelbestand_ns, sttr_id, uitv_dmn_id, regel_type,
                                bereik, gegevens_type, nen3610_id, activiteit_urn,
                                label, toelichting, opties, optie_type, prioriteit,
                                dmn_element_id)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                                   (SELECT id FROM i2a.dmn_element
                                     WHERE regelbestand_ns = %s AND dmn_id = %s))
                           -- Het predicaat MOET mee: uq_uitv_sttr_dmn is een
                           -- partiele unieke index, en Postgres herkent die
                           -- alleen als de inferentie hetzelfde filter draagt.
                           -- Zonder deze WHERE: "there is no unique or exclusion
                           -- constraint matching the ON CONFLICT specification".
                           ON CONFLICT (sttr_id, uitv_dmn_id)
                             WHERE sttr_id IS NOT NULL AND uitv_dmn_id IS NOT NULL
                           DO NOTHING""",
                        (rij["fsr"], r["sttr_id"], r["uitv_dmn_id"], r["regel_type"],
                         r["bereik"], r["gegevens_type"], r["nen3610_id"],
                         r["activiteit_urn"], r["label"], r["toelichting"],
                         r["opties"], r["optie_type"], r["prioriteit"],
                         rij["fsr"], r["input_dmn_id"]))
                cur.execute("UPDATE i2a.sttr_bestand SET geparsed_op = now() "
                            "WHERE sttr_id = %s", (rij["sttr_id"],))
            conn.commit()
            stats["bestanden"] += 1
            stats["regels"] += len(regels)
            stats["geo"] += sum(1 for r in regels if r["nen3610_id"])
            stats["herbruik"] += sum(1 for r in regels if r["activiteit_urn"])
            stats["bereik"] += sum(1 for r in regels if r["bereik"])
            stats["label"] += sum(1 for r in regels if r["label"])
            stats["opties"] += sum(1 for r in regels if r["opties"])
            if i % 500 == 0:
                console.print(f"    {i}/{len(rijen)} — {stats['regels']} regels")
    finally:
        conn.close()
    console.print(f"  [green]{stats['bestanden']} bestanden · {stats['regels']} regels "
                  f"· {stats['geo']} geoVerwijzing · {stats['herbruik']} herbruikbaar "
                  f"· {stats['bereik']} met bereik · {stats['label']} met tekst "
                  f"· {stats['opties']} met antwoordopties[/green]")
    return stats
