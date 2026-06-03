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
# /v1/viewer/teksten (batch)
# ══════════════════════════════════════════════════════════

class TestTekstenBatch:
    def _vind_tekst_wids(self, limiet=3):
        """Verzamel tot `limiet` wids van nodes met heeft_tekst=True."""
        r = client.get(f"/v1/viewer/regelingen?x={UTR_X}&y={UTR_Y}")
        expr = r.json()["regelingen"][0]["expression"]
        r = client.get(f"/v1/viewer/regeling/{expr}/boom")
        wids: list[str] = []

        def walk(nodes):
            for node in nodes:
                if node.get("heeft_tekst"):
                    wids.append(node["wid"])
                if len(wids) >= limiet:
                    return
                walk(node.get("kinderen", []))

        walk(r.json()["boom"])
        return wids

    def test_batch_laadt_meerdere_teksten(self):
        wids = self._vind_tekst_wids()
        assert wids, "Geen nodes met tekst gevonden"

        r = client.post("/v1/viewer/teksten", json={"wids": wids})
        assert r.status_code == 200
        teksten = r.json()["teksten"]
        # Elke wid komt precies één keer terug, met niet-lege tekst.
        terug = {t["wid"]: t["tekst"] for t in teksten}
        for wid in wids:
            assert wid in terug
            assert len(terug[wid]) > 0
        assert len(teksten) == len(terug)  # geen dubbele wids

    def test_batch_lege_lijst(self):
        r = client.post("/v1/viewer/teksten", json={"wids": []})
        assert r.status_code == 200
        assert r.json()["teksten"] == []

    def test_batch_onbekende_wid_wordt_stil_overgeslagen(self):
        wids = self._vind_tekst_wids(limiet=1)
        assert wids
        r = client.post(
            "/v1/viewer/teksten",
            json={"wids": [wids[0], "niet-bestaand-wid"]},
        )
        assert r.status_code == 200
        terug = {t["wid"] for t in r.json()["teksten"]}
        assert wids[0] in terug
        assert "niet-bestaand-wid" not in terug


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
        assert "activiteitlocatieaanduidingen" in data
        assert "omgevingsnormen" in data
        assert "normwaarden" in data
        assert "ongetypeerde_locaties" in data
        assert "wro_bestemmingen" in data

    def test_geen_tophaak_alas(self):
        """Tophaak-activiteiten moeten gefilterd zijn uit ALA's.
        NB: sommige provinciale tophaken staan niet als is_tophaak in de data."""
        r = client.get(f"/v1/viewer/objecten?x={AMS_X}&y={AMS_Y}")
        tophaak_count = sum(
            1 for ala in r.json()["activiteitlocatieaanduidingen"]
            if "activiteit gereguleerd in" in ala["naam"].lower()
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

    def test_ala_velden(self):
        r = client.get(f"/v1/viewer/objecten?x={AMS_X}&y={AMS_Y}")
        alas = r.json()["activiteitlocatieaanduidingen"]
        assert len(alas) > 0
        ala = alas[0]
        assert "naam" in ala
        assert "kwalificatie" in ala
        assert "groep" in ala
        assert "regelingen" in ala
        assert isinstance(ala["regelingen"], list)

    def test_ongetypeerde_locaties_velden(self):
        r = client.get(f"/v1/viewer/objecten?x={AMS_X}&y={AMS_Y}")
        locs = r.json()["ongetypeerde_locaties"]
        if not locs:
            pytest.skip("Geen ongetypeerde locaties op deze locatie")
        loc = locs[0]
        assert "identificatie" in loc
        assert "noemer" in loc
        assert "locatie_type" in loc

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


# ══════════════════════════════════════════════════════════
# /v1/viewer/regeling/{expression}/wijzigingen
# ══════════════════════════════════════════════════════════

from urllib.parse import quote as _q

# gm0353 (IJsselstein) — bekende regeling met 2 ontwerp-bronnen in p2pwijziging
# (zie ../../OCDviewer/docs/plans/complete/20260516-wijzigingen-overlay.md).
GM0353_EXPR = "/akn/nl/act/gm0353/2020/omgevingsplan/nld@2024-03-19;1"
GM0353_WORK = "/akn/nl/act/gm0353/2020/omgevingsplan"


def _wijz_url(expr: str) -> str:
    """URL-encoded path zoals de frontend hem aanroept (encodeURIComponent).
    Zonder encoding ziet de FastAPI-path-param de leading-slash niet als
    onderdeel van de expression."""
    return f"/v1/viewer/regeling/{_q(expr, safe='')}/wijzigingen"


class TestRegelmix:
    VERPLICHTE_VELDEN = (
        "bron_type", "bron_id", "regeling", "documenttype", "bestuurslaag",
        "artikel", "artikel_nummer", "artikel_opschrift", "hoofdstuk_nummer",
        "activiteit_naam", "activiteit_id", "inhoud",
    )

    def test_amsterdam_retourneert_regelmix(self):
        r = client.get(f"/v1/viewer/regelmix?x={AMS_X}&y={AMS_Y}")
        assert r.status_code == 200
        data = r.json()
        assert data["locatie"]["x"] == AMS_X
        assert len(data["regelmix"]) > 0

    def test_rijen_hebben_verplichte_velden(self):
        r = client.get(f"/v1/viewer/regelmix?x={AMS_X}&y={AMS_Y}")
        for rij in r.json()["regelmix"]:
            for veld in self.VERPLICHTE_VELDEN:
                assert veld in rij, f"Veld {veld} ontbreekt"

    def test_bevat_ow_rijen(self):
        r = client.get(f"/v1/viewer/regelmix?x={AMS_X}&y={AMS_Y}")
        bron_types = {rij["bron_type"] for rij in r.json()["regelmix"]}
        assert "ow" in bron_types

    def test_bron_type_alleen_ow_of_wro(self):
        r = client.get(f"/v1/viewer/regelmix?x={AMS_X}&y={AMS_Y}")
        for rij in r.json()["regelmix"]:
            assert rij["bron_type"] in ("ow", "wro")

    def test_inhoud_is_html_gestript(self):
        r = client.get(f"/v1/viewer/regelmix?x={AMS_X}&y={AMS_Y}")
        for rij in r.json()["regelmix"]:
            assert "<" not in (rij["inhoud"] or ""), \
                f"HTML niet gestript in {rij['bron_id']}: {rij['inhoud'][:80]}"

    def test_wro_rijen_op_locatie_met_wro_plan(self):
        """Op een locatie met een actief Wro-plan moet bron_type 'wro' voorkomen,
        met lege activiteit-velden."""
        # Vind een locatie met een Wro-plan via het regelingen-endpoint.
        coords = [(AMS_X, AMS_Y), (UTR_X, UTR_Y)]
        wro_coord = None
        for cx, cy in coords:
            rr = client.get(f"/v1/viewer/regelingen?x={cx}&y={cy}")
            if rr.json()["wro_plannen"]:
                wro_coord = (cx, cy)
                break
        if not wro_coord:
            pytest.skip("Geen Wro-plan op de testcoördinaten")

        r = client.get(f"/v1/viewer/regelmix?x={wro_coord[0]}&y={wro_coord[1]}")
        wro_rijen = [rij for rij in r.json()["regelmix"] if rij["bron_type"] == "wro"]
        # Een Wro-plan zonder tekst-objecten levert geen rijen; dan is er niets
        # te asserten over de vorm. Maar als er rijen zijn, moeten ze kloppen.
        for rij in wro_rijen:
            assert rij["activiteit_naam"] is None
            assert rij["activiteit_id"] is None
            assert rij["bron_id"]

    def test_limit_wordt_gerespecteerd(self):
        r = client.get(f"/v1/viewer/regelmix?x={AMS_X}&y={AMS_Y}&limit=5")
        assert r.status_code == 200
        ow_rijen = [rij for rij in r.json()["regelmix"] if rij["bron_type"] == "ow"]
        assert len(ow_rijen) <= 5

    def test_zee_locatie_geen_gemeentelijke_rijen(self):
        r = client.get(f"/v1/viewer/regelmix?x={ZEE_X}&y={ZEE_Y}")
        assert r.status_code == 200
        for rij in r.json()["regelmix"]:
            assert rij["bestuurslaag"] in (None, "rijk"), \
                f"Onverwachte bestuurslaag op zee: {rij['bestuurslaag']}"


class TestWijzigingen:
    def test_onbekende_expression_geeft_404(self):
        r = client.get(_wijz_url("/akn/nl/act/niet-bestaand"))
        assert r.status_code == 404

    def test_gm0353_response_structuur(self):
        r = client.get(_wijz_url(GM0353_EXPR))
        assert r.status_code == 200
        data = r.json()
        assert data["regelingWork"] == GM0353_WORK
        assert isinstance(data["wijzigingen"], list)

    def test_gm0353_heeft_bronnen(self):
        r = client.get(_wijz_url(GM0353_EXPR))
        wijzigingen = r.json()["wijzigingen"]
        # Mock-fixture en database hebben 2 ontwerpen bekend. Hou de assert
        # zacht (>= 1) zodat een latere extra bron de test niet breekt.
        assert len(wijzigingen) >= 1

    def test_wijziging_velden(self):
        r = client.get(_wijz_url(GM0353_EXPR))
        w = r.json()["wijzigingen"][0]
        for key in ("ontwerpbesluitId", "soort", "status", "opschrift",
                    "bekendOp", "beginInwerking", "bronhouder",
                    "documenttype", "isVervangRegeling",
                    "tekstElementen", "annotatieDeltas", "locatieDeltas"):
            assert key in w, f"Veld {key} ontbreekt"
        assert w["soort"] in ("ontwerp", "besluitversie")
        assert isinstance(w["tekstElementen"], list)
        assert isinstance(w["annotatieDeltas"], list)

    def test_tekst_elementen_gestript_naar_gewijzigd_plus_parents(self):
        """Strip-logica: alle wid-knopen die we leveren bevatten ofwel een
        renvooi-signaal, ofwel zijn een ancestor van zo'n knoop. Ongewijzigde
        broertjes/zusters horen er niet in."""
        r = client.get(_wijz_url(GM0353_EXPR))
        for w in r.json()["wijzigingen"]:
            tekst_els = w["tekstElementen"]
            assert len(tekst_els) > 0
            # Volle-boom-mirror zou ~1500 zijn; gestript verwachten we onder 100.
            assert len(tekst_els) < 100, \
                f"Strip lijkt niet te werken: {len(tekst_els)} elementen"

    def test_annotaties_gefilterd_op_imow_types(self):
        r = client.get(_wijz_url(GM0353_EXPR))
        toegestaan = {"activiteit", "gebiedsaanwijzing", "omgevingsnorm",
                       "omgevingswaarde", "locatie", "tekstdeel"}
        for w in r.json()["wijzigingen"]:
            for ad in w["annotatieDeltas"]:
                assert ad["type"] in toegestaan, \
                    f"SKOS-pipeline-type lekt door: {ad['type']}"

    def test_vervang_regeling_wordt_uitgesloten(self):
        r = client.get(_wijz_url(GM0353_EXPR))
        for w in r.json()["wijzigingen"]:
            assert w["isVervangRegeling"] is False
