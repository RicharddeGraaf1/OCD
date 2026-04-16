"""Shared utilities for the OCD loader."""

import re


def strip_xml(text: str | None) -> str | None:
    """Strip XML/STOP tags from tekst_element inhoud to get plain text.

    Used to populate the inhoud_plain column for full-text search.
    """
    if not text:
        return None
    clean = re.sub(r"<\?xml[^?]*\?>", "", text)
    clean = re.sub(r"<[^>]+>", " ", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean if clean else None
