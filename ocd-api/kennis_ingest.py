#!/usr/bin/env python3
"""Chunk + embed de Omgevingswet-vault naar een lokale Chroma-store.

Vault-pad: C:\\GIT\\OmgevingswetKnowledgeBase\\vault_v1\\
Doelmap: D:\\OCDChroma\\ (persistent, los van OCD-API code)

Strategie:
- Voor elke .md in concepts/, entities/, sources/, analysis/:
  - frontmatter strippen
  - splitsen op '^## ' headings
  - eerste chunk = pre-heading intro (vaak overview)
  - per chunk: id = "{path}#{section}", text = "Titel + heading + content"
- Embedden via Ollama nomic-embed-text
- Opslaan in Chroma collection "vault_kennis" met metadata
  (path, title, section, tags)

Gebruik:
    python kennis_ingest.py
    python kennis_ingest.py --vault /path/to/vault_v1 --reset
"""

import argparse
import json
import logging
import re
import sys
from pathlib import Path

import httpx

try:
    import chromadb
except ImportError:
    print("ERROR: chromadb not installed. Run: pip install chromadb")
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DEFAULT_VAULT = Path(r"C:\GIT\OmgevingswetKnowledgeBase\vault_v1")
DEFAULT_DB = Path(r"D:\OCDChroma")
COLLECTION = "vault_kennis"
EMBED_MODEL = "nomic-embed-text"
OLLAMA_URL = "http://localhost:11434"

SUBDIRS = ["concepts", "entities"]  # B: skip analysis/ + sources/ (te technisch/meta)

# Titels die puur technisch-administratief zijn en geen toelichting bij
# eindgebruiker-vragen geven. Worden helemaal overgeslagen.
TITLE_BLACKLIST = {
    "Aansluitpunt en aansluiting",
    "Besluitversie",
    "FRBR-identificatie",
    "Functionele structuur",
    "IntIoRef en ExtIoRef",
    "Pons",
    "Regelbeheerobject",
    "Regelingsgebied",
    "Renvooi-annotatie",
    "basisgeo-id en de Locatie-GIO-keten",
    "locatieSelectie en aggregaat-geometrieen",
}

# Section-headings die technische details bevatten en geen uitleg.
# Match case-insensitive op begin van de section-string.
SECTION_BLACKLIST_PREFIXES = (
    "technisch",
    "xsd",
    "zip",
    "concrete voorbeelden",
    "voorbeelden uit",
    "citaten",
    "verschilanalyse",
    "model-impact",
    "kernbeweringen",
    "geciteerde",
    "wat dit oplost",
    "gerelateerde",
)


def is_skipped_section(section: str) -> bool:
    s = section.strip().lower()
    return any(s.startswith(p) for p in SECTION_BLACKLIST_PREFIXES)

_FM_RE = re.compile(r"^---\n.*?\n---\n", re.DOTALL)
_HEADING_RE = re.compile(r"^## +(.+?)\s*$", re.MULTILINE)


def strip_frontmatter(text: str) -> tuple[str, dict]:
    """Verwijder YAML frontmatter; returnt (body, frontmatter_dict)."""
    m = _FM_RE.match(text)
    fm: dict = {}
    if m:
        fm_text = m.group(0).strip("-\n")
        # Heel simpele yaml-parse (key: value of key: [list])
        for line in fm_text.split("\n"):
            line = line.strip()
            if not line or ":" not in line:
                continue
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip().strip('"').strip("'")
        text = text[m.end():]
    return text, fm


def chunk_markdown(text: str) -> list[dict]:
    """Splits markdown body op ^## headings.
    Returnt lijst van {section, content}. Eerste chunk = pre-heading intro.
    """
    chunks: list[dict] = []
    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        # Geen ## headings — hele tekst als één chunk
        return [{"section": "_intro", "content": text.strip()}]

    # Pre-eerste-heading
    intro = text[:matches[0].start()].strip()
    if intro:
        chunks.append({"section": "_intro", "content": intro})

    for i, m in enumerate(matches):
        section = m.group(1).strip()
        start = m.end()
        end = matches[i+1].start() if i+1 < len(matches) else len(text)
        content = text[start:end].strip()
        if content:
            chunks.append({"section": section, "content": content})
    return chunks


def embed_via_ollama(text: str) -> list[float] | None:
    """Roep Ollama /api/embeddings aan. Returnt None bij fout."""
    try:
        resp = httpx.post(
            f"{OLLAMA_URL}/api/embeddings",
            json={"model": EMBED_MODEL, "prompt": text},
            timeout=60.0,
        )
        resp.raise_for_status()
        return resp.json().get("embedding")
    except Exception as e:
        logger.warning(f"Embedding failed: {e}")
        return None


def process_vault(vault: Path, db_path: Path, reset: bool = False) -> int:
    """Lees vault, chunkt, embeddt, slaat op. Returnt aantal chunks."""
    client = chromadb.PersistentClient(path=str(db_path))
    if reset and COLLECTION in [c.name for c in client.list_collections()]:
        client.delete_collection(COLLECTION)
        logger.info(f"Verwijderde bestaande collection {COLLECTION!r}")
    coll = client.get_or_create_collection(
        name=COLLECTION,
        metadata={"description": "Omgevingswet vault chunks", "embed_model": EMBED_MODEL},
    )

    ids, embeddings, documents, metadatas = [], [], [], []
    total_files = 0
    total_chunks = 0
    skipped_chunks = 0

    for subdir in SUBDIRS:
        d = vault / subdir
        if not d.is_dir():
            logger.info(f"Skipping {subdir!r} (niet aanwezig)")
            continue
        for md in sorted(d.glob("*.md")):
            title = md.stem
            if title in TITLE_BLACKLIST:
                logger.info(f"  SKIP (blacklist): {subdir}/{md.name}")
                continue
            total_files += 1
            try:
                raw = md.read_text(encoding="utf-8")
            except Exception as e:
                logger.warning(f"Kan {md.name!r} niet lezen: {e}")
                continue
            body, fm = strip_frontmatter(raw)
            tags = fm.get("tags", "").strip("[]").replace('"', "").replace("'", "")
            page_type = fm.get("type", subdir.rstrip("s"))

            chunks = chunk_markdown(body)
            rel_path = md.relative_to(vault).as_posix()

            for ch in chunks:
                if len(ch["content"]) < 50:
                    skipped_chunks += 1
                    continue  # te kort, niet zinvol
                if is_skipped_section(ch["section"]):
                    skipped_chunks += 1
                    continue  # technische section
                # Geef chunk-tekst context: titel + sectie + content
                embed_text = f"{title}\n{ch['section']}\n\n{ch['content']}"
                emb = embed_via_ollama(embed_text)
                if emb is None:
                    skipped_chunks += 1
                    continue
                # Chunk-ID moet uniek en stabiel zijn
                safe_section = re.sub(r"\W+", "-", ch["section"])[:80]
                chunk_id = f"{rel_path}#{safe_section}"

                ids.append(chunk_id)
                embeddings.append(emb)
                documents.append(ch["content"][:2000])  # cap voor opslag
                metadatas.append({
                    "path": rel_path,
                    "title": title,
                    "section": ch["section"],
                    "type": page_type,
                    "tags": tags,
                    "subdir": subdir,
                })
                total_chunks += 1
            logger.info(f"  {rel_path}: {len(chunks)} chunks")

    if not ids:
        logger.warning("Geen chunks om op te slaan.")
        return 0

    # Batch-upsert (Chroma verwacht id's uniek; bestaande worden overschreven)
    BATCH = 100
    for i in range(0, len(ids), BATCH):
        coll.upsert(
            ids=ids[i:i+BATCH],
            embeddings=embeddings[i:i+BATCH],
            documents=documents[i:i+BATCH],
            metadatas=metadatas[i:i+BATCH],
        )
    logger.info(
        f"Klaar: {total_files} files, {total_chunks} chunks opgeslagen, "
        f"{skipped_chunks} skipped. Collection: {COLLECTION} ({coll.count()} totaal in DB)"
    )
    return total_chunks


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--vault", type=Path, default=DEFAULT_VAULT)
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("--reset", action="store_true", help="Wis collection eerst")
    args = p.parse_args()

    if not args.vault.is_dir():
        logger.error(f"Vault niet gevonden: {args.vault}")
        sys.exit(1)

    args.db.mkdir(parents=True, exist_ok=True)
    n = process_vault(args.vault, args.db, args.reset)
    print(f"\nDone. {n} chunks opgeslagen in {args.db / 'chroma.sqlite3'}")


if __name__ == "__main__":
    main()
