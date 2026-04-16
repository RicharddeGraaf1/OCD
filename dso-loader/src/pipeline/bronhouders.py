"""Bronhouder-input voor de pipeline.

JSON-formaat: `{"<code>": "<naam>", ...}` waarbij <code> een CBS-code is voor
gemeenten (4 cijfers, evt. met leading zero), of een pv-/ws-code voor
provincies/waterschappen.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal


BronhouderType = Literal["gemeente", "provincie", "waterschap", "rijk"]


@dataclass(frozen=True)
class Bronhouder:
    """Een bronhouder zoals de pipeline hem nodig heeft.

    `code`           — CBS-code voor gemeenten (bv. "0344"), pv-code (bv. "pv26"),
                       ws-code (bv. "ws-aa-en-maas"), of rijk-code.
    `naam`           — leesbare naam.
    `type`           — gemeente | provincie | waterschap | rijk.
    `overheid_code`  — code zoals DSO/IMTR die kent: gemeenten worden geprefixt
                       met 'gm' (gm0344), provincies blijven pv26, etc.
    """

    code: str
    naam: str
    type: BronhouderType = "gemeente"

    @property
    def overheid_code(self) -> str:
        if self.type == "gemeente":
            return f"gm{self.code}"
        return self.code


def _infer_type(code: str) -> BronhouderType:
    if code.startswith("pv"):
        return "provincie"
    if code.startswith("ws") or code.startswith("hh") or code.startswith("hd"):
        return "waterschap"
    if code.startswith("mn") or code == "rijk":
        return "rijk"
    return "gemeente"


def load_bronhouders(source: str | Path | dict | Iterable[Bronhouder]) -> list[Bronhouder]:
    """Accept a JSON path, a {code: naam} dict, or an iterable of Bronhouders."""
    if isinstance(source, (str, Path)):
        data = json.loads(Path(source).read_text(encoding="utf-8"))
        return [Bronhouder(code=c, naam=n, type=_infer_type(c)) for c, n in data.items()]
    if isinstance(source, dict):
        return [Bronhouder(code=c, naam=n, type=_infer_type(c)) for c, n in source.items()]
    return list(source)


def filter_by_type(bronhouders: list[Bronhouder], *types: BronhouderType) -> list[Bronhouder]:
    """Filter helper voor ketens die alleen bepaalde overheidstypen ondersteunen."""
    return [b for b in bronhouders if b.type in types]
