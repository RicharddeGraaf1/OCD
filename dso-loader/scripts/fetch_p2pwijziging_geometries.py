"""
Backfill van `p2pwijziging.locatie_delta.geometrie` per regeling.

CLI:
    python -m scripts.fetch_p2pwijziging_geometries <regeling-work>

Status (2026-05-17): SKELET — implementatie pending.

Het schema `p2pwijziging.locatie_delta` slaat alleen `locatie_id` op, niet
`geometrieIdentificatie`. Het DSO-endpoint `/geometrieen/{uuid}` verwacht
de geometrie-uuid, niet de locatie-uuid. Twee implementatie-paden:

  Pad A — Schema-uitbreiding:
    1. ALTER TABLE p2pwijziging.locatie_delta ADD COLUMN geometrie_identificatie TEXT
    2. Loader (src/loaders/ontwerp_loader.py::_store_locaties) bijwerken om
       `loc.get("geometrieIdentificatie")` ook op te slaan
    3. Hier-onder: per rij met geometrie IS NULL → /geometrieen/{geometrie_identificatie}

  Pad B — Re-load via ontwerp/besluit-detail:
    1. Voor één regeling-work: itereer over alle ontwerpbesluit_id's
    2. Roep DSO /ontwerpregelingen/{technisch_id} of /besluitversies/{...}
       opnieuw aan om de oorspronkelijke locatie-payload (incl.
       geometrieIdentificatie) te krijgen
    3. Per locatie: extract geometrieIdentificatie + fetch geometrie
    4. UPDATE p2pwijziging.locatie_delta SET geometrie = ... WHERE locatie_id = ...

Pad A is sneller per call (1 round-trip per locatie) maar vereist een
schema-migratie en data-her-load voor alle 214 huidige bronnen.
Pad B is trager (2 round-trips per locatie) maar werkt zonder schema-wijziging.

TODO: kies één pad en implementeer.
"""

import sys


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        print("\nFOUT: regeling-work argument ontbreekt.")
        return 2

    regeling_work = sys.argv[1]
    print(f"[fetch_p2pwijziging_geometries] regeling: {regeling_work}")
    print("Skelet — zie module-docstring voor implementatie-opties.")
    print("Geen wijzigingen aan de database gemaakt.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
