"""Fase 2.1 — meet de retrieval van de KERNEL (`/v1/regelteksten-bij-vraag`)
tegen dezelfde ground-truth als de bot-retrieval-eval (`retrieval_cases.json`).

De harde poort van plan (2): haalt de kernel de brede recall van de bot? Vergelijk
de score hier met de bot-retrieval-eval (gewogen SKOS aan = 61,5%).

Scoring identiek aan omgevingsbot `run_retrieval_eval.py`, maar gemapt op de
kernel-respons: document_titel←regeling, titel←artikel_opschrift/artikel,
tekst←inhoud. Geen LLM; deterministisch.

Gebruik:
  OCD_API_BASE=http://127.0.0.1:8002 python tools/kernel_retrieval_eval.py
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import httpx

API = os.getenv("OCD_API_BASE", "http://127.0.0.1:8002")
ENDPOINT = os.getenv("OCD_ENDPOINT", "/v1/regelteksten-bij-vraag")
CASES = Path(r"C:/GIT/omgevingsbot.nl/backend/tests/evaluation/retrieval_cases.json")
_COORD = re.compile(r"^\s*([\d.]+)\s*,\s*([\d.]+)\s*$")


def _payload(c: dict) -> dict:
    base = {"question": c["question"], "max_concepts": 5, "max_regelteksten": 20}
    m = _COORD.match(c["location"])
    if m:
        return {**base, "x": float(m.group(1)), "y": float(m.group(2))}
    return {**base, "location": c["location"]}


def _match(pattern: str, *texts) -> bool:
    rx = re.compile(pattern, re.IGNORECASE)
    return any(t and rx.search(t) for t in texts)


def _map_hit(h: dict) -> dict:
    """Kernel-RegeltekstHit → eval-velden."""
    return {
        "document_titel": h.get("regeling"),
        "titel": h.get("artikel_opschrift") or h.get("artikel"),
        "tekst": h.get("inhoud"),
    }


def evaluate(case: dict, hits: list[dict]) -> dict:
    checks = []
    for spec in case.get("expected_in_topk", []):
        k = spec.get("k", 10)
        kp = spec.get("kop_pad_pattern") or spec.get("artikel_pattern")
        hit_idx = None
        for i, h in enumerate(hits[:k]):
            if _match(spec["regeling_match"], h.get("document_titel")) and (
                not kp or _match(kp, h.get("titel"), h.get("tekst"))
            ):
                hit_idx = i + 1
                break
        checks.append({"passed": hit_idx is not None, "hit_index": hit_idx,
                       "regeling": spec["regeling_match"]})
    for spec in case.get("expected_not_in_topk", []):
        k = spec.get("k", 3)
        viol = [i + 1 for i, h in enumerate(hits[:k])
                if _match(spec["regeling_match"], h.get("document_titel"))]
        checks.append({"passed": not viol, "violations": viol, "regeling": spec["regeling_match"]})
    n = len(checks)
    npass = sum(1 for c in checks if c["passed"])
    return {"checks": checks, "score": (npass / n * 100) if n else 0.0,
            "n_pass": npass, "n": n, "all": npass == n and n > 0}


def main():
    cases = json.loads(CASES.read_text(encoding="utf-8"))
    print(f"Kernel-retrieval-eval: {len(cases)} cases tegen {API}\n")
    results = []
    with httpx.Client() as client:
        for c in cases:
            try:
                d = client.post(f"{API}{ENDPOINT}", json=_payload(c), timeout=60).json()
                hits = [_map_hit(h) for h in d.get("regelteksten", [])]
            except Exception as e:
                print(f"  [!] {c['id']}: {e}")
                results.append({"id": c["id"], "score": 0.0, "status": "ERROR"})
                continue
            ev = evaluate(c, hits)
            st = "PASS" if ev["all"] else ("PARTIAL" if ev["n_pass"] else "FAIL")
            results.append({"id": c["id"], "score": ev["score"], "status": st,
                            "n_hits": len(hits), "n_pass": ev["n_pass"], "n": ev["n"]})
            print(f"  {st:7s} {ev['score']:5.1f}% ({ev['n_pass']}/{ev['n']}) hits={len(hits):2d}  {c['id']}")
    n = len(results)
    avg = sum(r["score"] for r in results) / n if n else 0
    P = sum(1 for r in results if r["status"] == "PASS")
    p = sum(1 for r in results if r["status"] == "PARTIAL")
    F = sum(1 for r in results if r["status"] == "FAIL")
    print(f"\nKERNEL: n={n} PASS={P} PARTIAL={p} FAIL={F} avg={avg:.1f}%")
    print("Bot-retrieval (gewogen SKOS aan) = 61.5% — poort 2.1: kernel >= bot?")
    out = Path(__file__).resolve().parent.parent / "kernel_retrieval_results.json"
    out.write_text(json.dumps({"avg": round(avg, 1), "results": results}, indent=2, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
