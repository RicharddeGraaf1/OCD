"""Bouw/ververs de viewer-golden-set — regressie-vangnet voor de OCD-viewer.

De viewer heeft (anders dan de bot) geen eval. Dit script maakt een SNAPSHOT van
wat `/v1/regelteksten-bij-vraag` per representatieve vraag teruggeeft: de velden
die de viewer-UI feitelijk gebruikt (objectfilter + kaart/regelingen):

  - top-regelteksten  -> kaart/regelingen-filter
  - matched_concepts  -> chips
  - keywords (ScoredKeyword) + expanded_keywords -> objectpaneel-vinkjes

Gebruik:
  python tools/build_viewer_golden_set.py            # schrijf snapshot
  python tools/build_viewer_golden_set.py --diff      # vergelijk met bestaande snapshot

De casuslijst wordt hergebruikt uit de bot-retrieval-eval (zelfde locaties/vragen),
zodat bot- en viewer-vangnet op dezelfde gevallen meten.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

import httpx

API = os.getenv("OCD_API_BASE", "http://127.0.0.1:8001")
HERE = Path(__file__).resolve().parent
SNAPSHOT = HERE.parent / "viewer_golden_set.json"
# casuslijst uit de bot-repo (zelfde 13 gevallen als de retrieval-eval)
BOT_CASES = Path(
    r"C:/GIT/omgevingsbot.nl/backend/tests/evaluation/retrieval_cases.json"
)
TOP_N = 10


def _load_cases() -> list[dict]:
    raw = json.loads(BOT_CASES.read_text(encoding="utf-8"))
    return [{"id": c["id"], "question": c["question"], "location": c["location"]} for c in raw]


_COORD = re.compile(r"^\s*([\d.]+)\s*,\s*([\d.]+)\s*$")


def _payload(case: dict) -> dict:
    base = {"question": case["question"], "max_concepts": 5, "max_regelteksten": 20}
    # De viewer-endpoint kent geen RD-coord-string in `location` (anders dan de bot);
    # parse "x, y" hier naar expliciete x/y. Zie delta-note R1.
    m = _COORD.match(case["location"])
    if m:
        return {**base, "x": float(m.group(1)), "y": float(m.group(2))}
    return {**base, "location": case["location"]}


def _snapshot_case(client: httpx.Client, case: dict) -> dict:
    resp = client.post(
        f"{API}/v1/regelteksten-bij-vraag",
        json=_payload(case),
        timeout=60,
    )
    resp.raise_for_status()
    d = resp.json()
    rts = d.get("regelteksten", [])
    return {
        "id": case["id"],
        "question": case["question"],
        "location": case["location"],
        "n_regelteksten": len(rts),
        "top_regelingen": sorted({(r.get("regeling") or r.get("regeling_expression") or "?")
                                  for r in rts[:TOP_N]}),
        "top_hits": [
            {"regeling": r.get("regeling"), "artikel": r.get("artikel_nummer") or r.get("artikel"),
             "join_pad": r.get("join_pad"), "relevantie": r.get("relevantie")}
            for r in rts[:TOP_N]
        ],
        "matched_concepts": [{"naam": m.get("naam"), "score": m.get("score")}
                             for m in d.get("matched_concepts", [])],
        "expanded_keywords": sorted(d.get("expanded_keywords", [])),
        "scored_keywords": [{"term": k.get("term"), "relevantie": k.get("relevantie"),
                             "is_actie": k.get("is_actie")}
                            for k in d.get("keywords", [])],
    }


def build() -> list[dict]:
    cases = _load_cases()
    out = []
    with httpx.Client() as client:
        for i, case in enumerate(cases, 1):
            print(f"[{i}/{len(cases)}] {case['id']}")
            try:
                out.append(_snapshot_case(client, case))
            except Exception as e:
                print(f"    [!] {e}")
                out.append({"id": case["id"], "error": str(e)})
    return out


def diff(new: list[dict]) -> None:
    if not SNAPSHOT.exists():
        print("Geen bestaande snapshot om tegen te diffen.")
        return
    old = {c["id"]: c for c in json.loads(SNAPSHOT.read_text(encoding="utf-8"))}
    for c in new:
        o = old.get(c["id"])
        if not o:
            print(f"+ NIEUW: {c['id']}")
            continue
        for field in ("top_regelingen", "expanded_keywords"):
            so, sn = set(o.get(field, [])), set(c.get(field, []))
            if so != sn:
                print(f"~ {c['id']} :: {field}")
                if sn - so:
                    print(f"    + {sorted(sn - so)}")
                if so - sn:
                    print(f"    - {sorted(so - sn)}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--diff", action="store_true", help="Vergelijk met bestaande snapshot i.p.v. overschrijven")
    args = p.parse_args()
    new = build()
    if args.diff:
        diff(new)
    else:
        SNAPSHOT.write_text(json.dumps(new, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nSnapshot bewaard: {SNAPSHOT}  ({len(new)} cases)")


if __name__ == "__main__":
    main()
