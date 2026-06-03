"""Kennislaag-endpoint: semantische zoek over de Omgevingswet-vault.

`/v1/kennis?q=...&top_k=5` retourneert vault-chunks (concepten, entiteiten,
analyses) die qua betekenis dichtbij de vraag liggen. Gebruikt nomic-embed-text
via Ollama voor de query, en Chroma voor de top-k similarity search.

Bedoeling: de bot kan deze als 2e retrieval-laag aanroepen naast DSO-data.
Voor "wat is een gebiedsaanwijzing?" (definitievraag) zijn de hits hier
relevanter dan in de DSO-regels-tabel.
"""

import logging
import os
from typing import List, Optional

import httpx
from fastapi import APIRouter, Query

logger = logging.getLogger("ocd_api.kennis")

_CHROMA_PATH = os.getenv("OCD_KENNIS_DB", r"D:\OCDChroma")
_COLLECTION = "vault_kennis"
_EMBED_MODEL = os.getenv("OCD_KENNIS_EMBED_MODEL", "nomic-embed-text")
_OLLAMA_URL = os.getenv("OCD_EXPAND_URL", "http://localhost:11434").rstrip("/")
_TIMEOUT = 30.0

router = APIRouter()

# Lazy-init zodat OCD-API kan opstarten ook als Chroma nog niet beschikbaar is.
_client = None
_collection = None


def _get_collection():
    global _client, _collection
    if _collection is not None:
        return _collection
    try:
        import chromadb
    except ImportError:
        raise RuntimeError("chromadb not installed")
    _client = chromadb.PersistentClient(path=_CHROMA_PATH)
    _collection = _client.get_collection(_COLLECTION)
    return _collection


def _embed_query(q: str) -> Optional[List[float]]:
    try:
        resp = httpx.post(
            f"{_OLLAMA_URL}/api/embeddings",
            json={"model": _EMBED_MODEL, "prompt": q},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json().get("embedding")
    except Exception as e:
        logger.warning(f"embed_query failed: {e}")
        return None


@router.get("/v1/kennis")
def kennis_endpoint(
    q: str = Query(..., min_length=2, max_length=500),
    top_k: int = Query(5, ge=1, le=20),
    type_filter: Optional[str] = Query(None, description="concept | entity | source | analysis"),
):
    """Semantische zoek over de vault. Returnt top_k chunks met titel + content + bron."""
    emb = _embed_query(q)
    if emb is None:
        return {"q": q, "results": [], "error": "embedding failed"}

    where = {"subdir": type_filter + "s"} if type_filter else None
    try:
        coll = _get_collection()
    except Exception as e:
        return {"q": q, "results": [], "error": f"chroma not available: {e}"}

    try:
        res = coll.query(
            query_embeddings=[emb],
            n_results=top_k,
            where=where,
        )
    except Exception as e:
        return {"q": q, "results": [], "error": f"query failed: {e}"}

    results = []
    ids = res.get("ids", [[]])[0]
    docs = res.get("documents", [[]])[0]
    metas = res.get("metadatas", [[]])[0]
    dists = res.get("distances", [[]])[0]

    for i, doc in enumerate(docs):
        m = metas[i] if i < len(metas) else {}
        # Cosine distance → similarity score (lager dist = beter)
        dist = dists[i] if i < len(dists) else None
        sim = (1.0 - dist) if isinstance(dist, (int, float)) else None
        results.append({
            "id": ids[i] if i < len(ids) else "",
            "title": m.get("title", ""),
            "section": m.get("section", ""),
            "path": m.get("path", ""),
            "type": m.get("type", ""),
            "tags": m.get("tags", ""),
            "content": doc,
            "similarity": round(sim, 4) if sim is not None else None,
        })

    return {"q": q, "results": results, "n": len(results)}
