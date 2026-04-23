"""Integration tests for OCDviewer FastAPI endpoints.

Tests draaien tegen de live OCD-database (read-only).
Vereist: PostgreSQL op localhost:5434 met OCD-data.

Run: pytest test_viewer.py -v
"""

import pytest
from fastapi.testclient import TestClient
from main import app
from db import pool

# Open de connection pool voor tests
pool.open()

client = TestClient(app)

# ── Fixtures ──

# Keizersgracht 1, Amsterdam (RD)
AMS_X, AMS_Y = 121687, 487316
# Domplein 1, Utrecht (RD)
UTR_X, UTR_Y = 136411, 456458
# Midden in de Noordzee (geen data)
ZEE_X, ZEE_Y = 50000, 600000


# ══════════════════════════════════════════════════════════
# /v1/viewer/regelingen
# ══════════════════════════════════════════════════════════

class TestRegelingen:
    def test_amsterdam_retourneert_regelingen(self):
        r = client.get(f"/v1/viewer/regelingen?x={AMS_X}&y={AMS_Y}")
        assert r.status_code == 200
        data = r.json()
        assert len(data["regelingen"]) > 0
        assert data["locatie"]["x"] == AMS_X

    def test_regelingen_bevatten_verwachte_velden(self):
        r = client.get(f"/v1/viewer/regelingen?x={AMS_X}&y={AMS_Y}")
        reg = r.json()["regelingen"][0]
        assert "expression" in reg
        assert "titel" in reg
        assert "type" in reg
        assert "bronhouder_naam" in reg
        assert "bestuurslaag" in reg

    def test_regelingen_zijn_gededupliceerd(self):
        r = client.get(f"/v1/viewer/regelingen?x={AMS_X}&y={AMS_Y}")
        titels = [reg["titel"] for reg in r.json()["regelingen"]]
        assert len(titels) == len(set(titels)), "Dubbele titels gevonden"

    def test_omgevingsplan_amsterdam_aanwezig(self):
        r = client.get(f"/v1/viewer/regelingen?x={AMS_X}&y={AMS_Y}")
        titels = [reg["titel"] for reg in r.json()["regelingen"]]
        assert any("Amsterdam" in t and "Omgevingsplan" in t for t in titels)

    def test_wro_plannen_zijn_objecten(self):
        r = client.get(f"/v1/viewer/regelingen?x={AMS_X}&y={AMS_Y}")
        wro = r.json()["wro_plannen"]
        assert isinstance(wro, list)
        if len(wro) > 0:
            assert "idn" in wro[0]
            assert "titel" in wro[0]
            assert "pons_status" in wro[0]

    def test_alleen_actieve_wro_plannen(self):
        r = client.get(f"/v1/viewer/regelingen?x={AMS_X}&y={AMS_Y}")
        for plan in r.json()["wro_plannen"]:
            assert plan["pons_status"] == "actief"

    def test_pons_is_boolean(self):
        r = client.get(f"/v1/viewer/regelingen?x={AMS_X}&y={AMS_Y}")
        assert isinstance(r.json()["pons_aanwezig"], bool)

    def test_zee_locatie_retourneert_alleen_rijksregelingen(self):
        """Midden in de Noordzee: alleen Rijks-regelingen, geen gemeente/provincie."""
        r = client.get(f"/v1/viewer/regelingen?x={ZEE_X}&y={ZEE_Y}")
        assert r.status_code == 200
        for reg in r.json()["regelingen"]:
            assert reg["bestuurslaag"] in (None, "rijk"), \
                f"Onverwachte bestuurslaag op zee: {reg['bestuurslaag']} ({reg['titel']})"

    def test_bestuurslagen_zijn_valide(self):
        r = client.get(f"/v1/viewer/regelingen?x={AMS_X}&y={AMS_Y}")
        valide = {"gemeente", "provincie", "waterschap", "rijk", None}
        for reg in r.json()["regelingen"]:
            assert reg["bestuurslaag"] in valide, f"Onverwachte bestuurslaag: {reg['bestuurslaag']}"


# ══════════════════════════════════════════════════════════
# /v1/viewer/regeling/{expression}/boom
# ══════════════════════════════════════════════════════════

class TestBoom:
    @pytest.fixture(autouse=True)
    def setup(self):
        """Haal de expression van het eerste document op."""
        r = client.get(f"/v1/viewer/regelingen?x={UTR_X}&y={UTR_Y}")
        regs = r.json()["regelingen"]
        assert len(regs) > 0
        self.expression = regs[0]["expression"]

    def test_boom_retourneert_nodes(self):
        r = client.get(f"/v1/viewer/regeling/{self.expression}/boom")
        assert r.status_code == 200
        data = r.json()
        assert len(data["boom"]) > 0

    def test_boom_is_correct_genest(self):
        """Top-level nodes moeten root-elementen zijn, niet Artikelen of Leden."""
        r = client.get(f"/v1/viewer/regeling/{self.expression}/boom")
        root_types = {node["type"] for node in r.json()["boom"]}
        assert "Lid" not in root_types, "Leden mogen niet op root-niveau staan"
        assert "Artikel" not in root_types or len(r.json()["boom"]) < 50, \
            "Te veel Artikelen op root-niveau — nesting is waarschijnlijk fout"

    def test_boom_bevat_verwachte_velden(self):
        r = client.get(f"/v1/viewer/regeling/{self.expression}/boom")
        node = r.json()["boom"][0]
        assert "wid" in node
        assert "type" in node
        assert "kinderen" in node
        assert "heeft_tekst" in node
        assert isinstance(node["kinderen"], list)

    def test_boom_tekst_is_lazy(self):
        """Tekst mag niet in de boom-response zitten (lazy loading)."""
        r = client.get(f"/v1/viewer/regeling/{self.expression}/boom")
        def check_no_text(nodes):
            for node in nodes:
                assert node.get("tekst") is None, \
                    f"Node {node['wid']} heeft tekst in de boom — zou lazy moeten zijn"
                check_no_text(node.get("kinderen", []))
        check_no_text(r.json()["boom"])

    def test_locatie_ids_zijn_strings(self):
        r = client.get(f"/v1/viewer/regeling/{self.expression}/boom")
        for lid in r.json()["locatie_ids"]:
            assert isinstance(lid, str)

    def test_regeling_metadata(self):
        r = client.get(f"/v1/viewer/regeling/{self.expression}/boom")
        reg = r.json()["regeling"]
        assert reg["expression"] == self.expression
        assert len(reg["titel"]) > 0

    def test_niet_bestaande_regeling_geeft_404(self):
        r = client.get("/v1/viewer/regeling/niet-bestaand/boom")
        assert r.status_code == 404


# ══════════════════════════════════════════════════════════
# /v1/viewer/tekst/{wid}
# ══════════════════════════════════════════════════════════

class TestTekst:
    def test_tekst_laden(self):
        # Eerst een boom laden om een wid te vinden
        r = client.get(f"/v1/viewer/regelingen?x={UTR_X}&y={UTR_Y}")
        expr = r.json()["regelingen"][0]["expression"]
        r = client.get(f"/v1/viewer/regeling/{expr}/boom")
        # Zoek een node met heeft_tekst=True
        def find_tekst_node(nodes):
            for node in nodes:
                if node.get("heeft_tekst"):
                    return node["wid"]
                result = find_tekst_node(node.get("kinderen", []))
                if result:
                    return result
            return None
        wid = find_tekst_node(r.json()["boom"])
        assert wid is not None, "Geen node met tekst gevonden"

        r = client.get(f"/v1/viewer/tekst/{wid}")
        assert r.status_code == 200
        assert r.json()["wid"] == wid
        assert len(r.json()["tekst"]) > 0

    def test_niet_bestaand_wid_geeft_404(self):
        r = client.get("/v1/viewer/tekst/niet-bestaand-wid")
        assert r.status_code == 404


# ══════════════════════════════════════════════════════════
# /v1/viewer/geometrie
# ══════════════════════════════════════════════════════════

class TestGeometrie:
    def test_geometrie_retourneert_geojson(self):
        # Haal locatie_ids op via boom
        r = client.get(f"/v1/viewer/regelingen?x={UTR_X}&y={UTR_Y}")
        expr = r.json()["regelingen"][0]["expression"]
        r = client.get(f"/v1/viewer/regeling/{expr}/boom")
        ids = r.json()["locatie_ids"]
        if not ids:
            pytest.skip("Geen locatie_ids voor deze regeling")

        r = client.get(f"/v1/viewer/geometrie?locatie_ids={','.join(ids[:3])}")
        assert r.status_code == 200
        data = r.json()
        assert data["type"] == "FeatureCollection"
        assert len(data["features"]) > 0

    def test_geometrie_is_in_rd(self):
        """Coördinaten moeten in EPSG:28992 zijn (> 10000), niet WGS84."""
        r = client.get(f"/v1/viewer/regelingen?x={UTR_X}&y={UTR_Y}")
        expr = r.json()["regelingen"][0]["expression"]
        r = client.get(f"/v1/viewer/regeling/{expr}/boom")
        ids = r.json()["locatie_ids"]
        if not ids:
            pytest.skip("Geen locatie_ids")

        r = client.get(f"/v1/viewer/geometrie?locatie_ids={ids[0]}")
        feature = r.json()["features"][0]
        # Eerste coördinaat extraheren (kan genest zijn)
        coords = feature["geometry"]["coordinates"]
        while isinstance(coords[0], list):
            coords = coords[0]
        assert coords[0] > 10000, f"Coördinaat {coords[0]} lijkt WGS84, verwacht RD (>10000)"

    def test_lege_ids_retourneert_lege_collection(self):
        r = client.get("/v1/viewer/geometrie?locatie_ids=")
        assert r.status_code == 200
        assert r.json()["features"] == []

    def test_feature_properties(self):
        r = client.get(f"/v1/viewer/regelingen?x={UTR_X}&y={UTR_Y}")
        expr = r.json()["regelingen"][0]["expression"]
        r = client.get(f"/v1/viewer/regeling/{expr}/boom")
        ids = r.json()["locatie_ids"]
        if not ids:
            pytest.skip("Geen locatie_ids")

        r = client.get(f"/v1/viewer/geometrie?locatie_ids={ids[0]}")
        props = r.json()["features"][0]["properties"]
        assert "identificatie" in props
        assert "locatie_type" in props


# ══════════════════════════════════════════════════════════
# /v1/viewer/objecten
# ══════════════════════════════════════════════════════════

class TestObjecten:
    def test_objecten_retourneert_categorieën(self):
        r = client.get(f"/v1/viewer/objecten?x={AMS_X}&y={AMS_Y}")
        assert r.status_code == 200
        data = r.json()
        assert "gebiedsaanwijzingen" in data
        assert "activiteiten" in data
        assert "normwaarden" in data
        assert "wro_bestemmingen" in data

    def test_geen_tophaak_activiteiten(self):
        """Tophaak-activiteiten moeten gefilterd zijn.
        NB: sommige provinciale tophaken staan niet als is_tophaak in de data."""
        r = client.get(f"/v1/viewer/objecten?x={AMS_X}&y={AMS_Y}")
        tophaak_count = sum(
            1 for act in r.json()["activiteiten"]
            if "activiteit gereguleerd in" in act["naam"].lower()
        )
        # Maximaal een paar mogen doorlekken (data-kwaliteit issue, niet API-bug)
        assert tophaak_count <= 5, \
            f"Te veel tophaak-activiteiten in resultaten: {tophaak_count}"

    def test_gebiedsaanwijzing_velden(self):
        r = client.get(f"/v1/viewer/objecten?x={AMS_X}&y={AMS_Y}")
        gas = r.json()["gebiedsaanwijzingen"]
        if not gas:
            pytest.skip("Geen gebiedsaanwijzingen")
        ga = gas[0]
        assert "type" in ga
        assert "naam" in ga
        assert "regeling" in ga

    def test_activiteit_velden(self):
        r = client.get(f"/v1/viewer/objecten?x={AMS_X}&y={AMS_Y}")
        acts = r.json()["activiteiten"]
        assert len(acts) > 0
        act = acts[0]
        assert "naam" in act
        assert "kwalificatie" in act
        assert "regeling" in act

    def test_zee_locatie_alleen_rijksobjecten(self):
        """Noordzee: alleen Rijks-gebiedsaanwijzingen, geen gemeentelijke."""
        r = client.get(f"/v1/viewer/objecten?x={ZEE_X}&y={ZEE_Y}")
        assert r.status_code == 200
        data = r.json()
        # Geen Wro-bestemmingen op zee
        assert len(data["wro_bestemmingen"]) == 0
        # Eventuele gebiedsaanwijzingen zijn Rijks (Noordzee-gerelateerd)
        for ga in data["gebiedsaanwijzingen"]:
            assert "Rijk" in ga["regeling"] or "Minister" in ga["regeling"] or "AMvB" in ga.get("documenttype", ""), \
                f"Niet-Rijks gebiedsaanwijzing op zee: {ga['naam']} ({ga['regeling'][:50]})"


# ══════════════════════════════════════════════════════════
# /v1/viewer/wro/{idn}/detail
# ══════════════════════════════════════════════════════════

class TestWroDetail:
    @pytest.fixture(autouse=True)
    def setup(self):
        r = client.get(f"/v1/viewer/regelingen?x={AMS_X}&y={AMS_Y}")
        wro = r.json()["wro_plannen"]
        if not wro:
            pytest.skip("Geen Wro-plannen op deze locatie")
        self.idn = wro[0]["idn"]

    def test_wro_detail_retourneert_plan(self):
        r = client.get(f"/v1/viewer/wro/{self.idn}/detail")
        assert r.status_code == 200
        data = r.json()
        assert data["plan"]["idn"] == self.idn
        assert len(data["plan"]["naam"]) > 0

    def test_bestemmingen_zijn_geojson_in_rd(self):
        r = client.get(f"/v1/viewer/wro/{self.idn}/detail?x={AMS_X}&y={AMS_Y}")
        best = r.json()["bestemmingen"]
        assert best["type"] == "FeatureCollection"
        if best["features"]:
            coords = best["features"][0]["geometry"]["coordinates"]
            while isinstance(coords[0], list):
                coords = coords[0]
            assert coords[0] > 10000, "Verwacht RD-coördinaten"

    def test_conv_veld_aanwezig(self):
        r = client.get(f"/v1/viewer/wro/{self.idn}/detail")
        conv = r.json()["conv"]
        assert "beschikbaar" in conv
        assert isinstance(conv["beschikbaar"], bool)

    def test_niet_bestaand_plan_geeft_404(self):
        r = client.get("/v1/viewer/wro/NL.IMRO.9999.BESTAATNIET-XX01/detail")
        assert r.status_code == 404


# ══════════════════════════════════════════════════════════
# /v1/viewer/conv/{expression}/boom
# ══════════════════════════════════════════════════════════

class TestConvBoom:
    def test_conv_boom_met_data(self):
        """Test met een Utrecht-plan waarvan we weten dat het conv-data heeft."""
        # 2e Daalsedijk heeft 97 conv-nodes
        r = client.get(f"/v1/viewer/regelingen?x={UTR_X}&y={UTR_Y}")
        wro = r.json()["wro_plannen"]
        # Zoek een plan met conv
        conv_expr = None
        for plan in wro:
            r2 = client.get(f"/v1/viewer/wro/{plan['idn']}/detail")
            if r2.json()["conv"]["beschikbaar"]:
                conv_expr = r2.json()["conv"]["expression"]
                break
        if not conv_expr:
            pytest.skip("Geen conv-data beschikbaar")

        r = client.get(f"/v1/viewer/conv/{conv_expr}/boom")
        assert r.status_code == 200
        # Kan 0 nodes zijn als het plan wel geregistreerd is maar geen tekst heeft
        assert "boom" in r.json()
        assert "regeling" in r.json()

    def test_niet_bestaande_conv_geeft_404(self):
        r = client.get("/v1/viewer/conv/niet-bestaand/boom")
        assert r.status_code == 404
