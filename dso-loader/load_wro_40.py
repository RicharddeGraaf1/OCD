"""Load Wro bestemmingsplannen for all 40 gemeenten in one pass."""
import os
os.environ["PYTHONIOENCODING"] = "utf-8"

from src.loaders.wro_pdok import load_wro_plans

GEMEENTEN = {
    "0344": "Utrecht",
    "0363": "Amsterdam",
    "0518": "Den Haag",
    "0599": "Rotterdam",
    "0014": "Groningen",
    "0034": "Almere",
    "0153": "Enschede",
    "0193": "Eindhoven",
    "0202": "Arnhem",
    "0268": "Nijmegen",
    "0307": "Amersfoort",
    "0362": "Amstelveen",
    "0392": "Haarlem",
    "0394": "Haarlemmermeer",
    "0402": "Hilversum",
    "0439": "Purmerend",
    "0457": "Zaanstad",
    "0473": "Leidschendam-Voorburg",
    "0484": "Alphen aan den Rijn",
    "0503": "Delft",
    "0546": "Leiden",
    "0569": "Zoetermeer",
    "0579": "Dordrecht",
    "0637": "Breda",
    "0654": "Tilburg",
    "0668": "'s-Hertogenbosch",
    "0687": "Venlo",
    "0757": "Maastricht",
    "0758": "Sittard-Geleen",
    "0772": "Heerlen",
    "0796": "Apeldoorn",
    "0855": "Zwolle",
    "0995": "Lelystad",
    "1680": "Leeuwarden",
    "0080": "Leiderdorp",
    "0632": "Gooise Meren",
    "0060": "Ameland",
    "0074": "Schiermonnikoog",
    "0093": "Terschelling",
    "0400": "Texel",
}

if __name__ == "__main__":
    load_wro_plans(GEMEENTEN)
