"""Unit-tests voor gio_zip-extractie (geen DB-rondje voor de extractie zelf).

Bouwt een mini-ZIP uit de Gemeentestad-voorbeeldbestanden in de vault en
parseert die. Voor de update-functie gebruiken we een MagicMock-conn.
"""
from pathlib import Path
import tempfile
import zipfile
from unittest.mock import MagicMock

from src.loaders.gio_zip import (
    extract_gio_geometrie_ids,
    extract_gio_naam,
    extract_gio_naam_informatieobject,
    update_geo_informatieobject_gids,
)

_GEO_NS = "https://standaarden.overheid.nl/stop/imop/geo/"
_DATA_NS = "https://standaarden.overheid.nl/stop/imop/data/"


def _build_download_zip(tmp_path: Path, gios: dict[str, tuple[str, str]]) -> Path:
    """Bouw een mini-Download-ZIP. gios = {io_folder: (frbr, naamInformatieObject)}.

    Per GIO een IO-<folder>/geom.gml (GeoInformatieObjectVaststelling + FRBR) en
    een IO-<folder>/Metadata.xml (InformatieObjectMetadata + naamInformatieObject).
    """
    zip_path = tmp_path / "download.zip"
    with zipfile.ZipFile(zip_path, "w") as z:
        for folder, (frbr, naam) in gios.items():
            z.writestr(
                f"{folder}/geom.gml",
                f'<geo:GeoInformatieObjectVaststelling xmlns:geo="{_GEO_NS}">'
                f"<geo:vastgesteldeVersie><geo:GeoInformatieObjectVersie>"
                f"<geo:FRBRExpression>{frbr}</geo:FRBRExpression>"
                f"</geo:GeoInformatieObjectVersie></geo:vastgesteldeVersie>"
                f"</geo:GeoInformatieObjectVaststelling>",
            )
            naam_xml = (f"<naamInformatieObject>{naam}</naamInformatieObject>"
                        if naam is not None else "")
            z.writestr(
                f"{folder}/Metadata.xml",
                f'<InformatieObjectMetadata xmlns="{_DATA_NS}">{naam_xml}'
                f"</InformatieObjectMetadata>",
            )
    return zip_path


class TestExtractGioNaamInformatieObject:
    def test_koppelt_frbr_aan_naam_via_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = _build_download_zip(Path(tmp), {
                "IO-aaa": ("/join/id/regdata/gm0014/2024/Centrumgebied/nld@1;1", "Centrumgebied"),
                "IO-bbb": ("/join/id/regdata/gm0014/2024/Voetpaden/nld@1;1", "voetpaden"),
            })
            result = extract_gio_naam_informatieobject(zip_path)
            assert result == {
                "/join/id/regdata/gm0014/2024/Centrumgebied/nld@1;1": "Centrumgebied",
                "/join/id/regdata/gm0014/2024/Voetpaden/nld@1;1": "voetpaden",
            }

    def test_gio_zonder_naam_blijft_weg(self):
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = _build_download_zip(Path(tmp), {
                "IO-ccc": ("/join/id/regdata/gm0014/2024/Naamloos/nld@1;1", None),
            })
            assert extract_gio_naam_informatieobject(zip_path) == {}

# Pad naar de Gemeentestad-voorbeeldbestanden in de vault
VAULT_OPDRACHT = Path(
    r"c:/GIT/OmgevingswetKnowledgeBase/vault_v1/raw/voorbeeldbestanden-stoptpod/Gemeentestad/opdracht"
)


def _build_test_zip(tmp_path: Path, files: list[str]) -> Path:
    """Maak een ZIP met de meegegeven bestanden uit de Gemeentestad-map."""
    zip_path = tmp_path / "test.zip"
    with zipfile.ZipFile(zip_path, "w") as z:
        for fname in files:
            src = VAULT_OPDRACHT / fname
            if not src.exists():
                continue
            z.write(src, arcname=fname)
    return zip_path


class TestExtractGioGeometrieIds:
    def test_geen_gml_levert_lege_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            # ZIP met alleen metadata-XML, geen GML
            zip_path = _build_test_zip(tmp_path, ["GIO001-Bedrijf_categorie_2.xml"])
            result = extract_gio_geometrie_ids(zip_path)
            assert result == {}

    def test_één_gio_levert_één_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            zip_path = _build_test_zip(tmp_path, ["Bedrijf_categorie_2.gml"])
            result = extract_gio_geometrie_ids(zip_path)
            assert len(result) == 1
            # FRBR-expression komt uit de XML
            assert any(
                frbr.endswith("Bedrijf_categorie_2/nld@2019-06-18;3520")
                for frbr in result.keys()
            )
            # basisgeo:id-waarde
            assert "50EA019E-3C96-4631-A76C-35BCF4D7AB6D" in result.values()

    def test_meerdere_gios(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            zip_path = _build_test_zip(
                tmp_path,
                [
                    "Bedrijf_categorie_2.gml",
                    "Centrumgebied.gml",
                    "Speelhal.gml",
                    "Welstand.gml",
                ],
            )
            result = extract_gio_geometrie_ids(zip_path)
            # Vier GIO-GML's → vier mappings
            assert len(result) == 4
            # Alle waardes zijn UUID-achtige strings (>= 32 chars)
            for uuid in result.values():
                assert len(uuid) >= 32

    def test_pons_telt_ook_als_gio(self):
        # PonsGebied.gml heeft ook GeoInformatieObjectVaststelling als root —
        # een Pons is op STOP-data-niveau gewoon een GIO. Geen filter-werk:
        # als een ExtIoRef ooit naar een Pons-FRBR wijst, willen we 'm
        # juist wel kunnen resolven.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            zip_path = _build_test_zip(
                tmp_path,
                ["Bedrijf_categorie_2.gml", "PonsGebied.gml"],
            )
            result = extract_gio_geometrie_ids(zip_path)
            assert len(result) == 2
            # Beide hebben een eigen UUID
            assert len(set(result.values())) == 2


class TestExtractGioNaam:
    def test_versie_naam_wordt_opgepakt(self):
        # Bedrijf_categorie_2 is een simpele TPOD-GIO met geo:naam op
        # versieniveau ('Bedrijf categorie 2').
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = _build_test_zip(Path(tmp), ["Bedrijf_categorie_2.gml"])
            result = extract_gio_naam(zip_path)
            assert len(result) == 1
            assert list(result.values()) == ["Bedrijf categorie 2"]

    def test_geen_gml_levert_lege_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = _build_test_zip(Path(tmp), ["GIO001-Bedrijf_categorie_2.xml"])
            assert extract_gio_naam(zip_path) == {}


class TestUpdateGeoInformatieobjectGids:
    def test_lege_mapping_levert_nul_counts(self):
        conn = MagicMock()
        result = update_geo_informatieobject_gids(conn, {})
        assert result == {"matched": 0, "niet_gevonden": 0, "unchanged": 0}

    def test_succesvolle_update_telt_matched(self):
        conn = MagicMock()
        cur = MagicMock()
        # Maak `with conn.cursor() as cur:` werken
        conn.cursor.return_value.__enter__.return_value = cur
        cur.rowcount = 1
        result = update_geo_informatieobject_gids(
            conn, {"frbr_x": "uuid_x", "frbr_y": "uuid_y"}
        )
        assert result["matched"] == 2
        assert result["niet_gevonden"] == 0
        assert result["unchanged"] == 0

    def test_niet_gevonden_telt_correct(self):
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        cur.rowcount = 0
        cur.fetchone.return_value = None  # FRBR niet in tabel
        result = update_geo_informatieobject_gids(conn, {"frbr_x": "uuid_x"})
        assert result["matched"] == 0
        assert result["niet_gevonden"] == 1
        assert result["unchanged"] == 0
