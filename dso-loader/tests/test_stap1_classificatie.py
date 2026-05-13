"""Unit-tests voor stap1-classificatie (geen DB-rondje).

Dekking: het herkennen van Lichaam/Bijlage/Toelichting buckets uit
wro-rooteigenschappen, zodat de viewer-tabs juist gevuld worden.
"""

from src.converter.stap1 import (
    _BUCKETS,
    _BUCKET_EID,
    _classify_root,
    _make_eid,
)


class TestClassifyRoot:
    def test_object_type_regels_geeft_lichaam(self):
        assert _classify_root(None, "Regels") == "Lichaam"

    def test_object_type_bijlage(self):
        assert _classify_root(None, "Bijlage") == "Bijlage"

    def test_object_type_toelichting(self):
        assert _classify_root(None, "Toelichting") == "Toelichting"

    def test_overig_master_titel_default_lichaam(self):
        assert _classify_root("Bestemmingsplan x", "Overig") == "Lichaam"

    def test_overig_naam_begint_met_bijlage(self):
        # IHR-loader stempelt rare titels soms als "Overig" terwijl de naam
        # wel "Bijlage" voorop heeft.
        assert _classify_root("Bijlagen bij toelichting 4", "Overig") == "Bijlage"

    def test_overig_naam_begint_met_toelichting(self):
        assert _classify_root("Toelichting 1", "Overig") == "Toelichting"

    def test_lege_input_lichaam(self):
        assert _classify_root(None, None) == "Lichaam"


class TestMakeEid:
    def test_geneste_chp_onder_container(self):
        assert _make_eid(1, "1", 0, "body") == "body__chp_1"

    def test_geneste_art_onder_chp(self):
        assert _make_eid(2, "1.2", 0, "body__chp_1") == "body__chp_1__art_1_2"

    def test_los_chp_zonder_parent(self):
        assert _make_eid(1, "3", 0, None) == "chp_3"

    def test_volgnummer_fallback_zonder_nummer(self):
        assert _make_eid(2, None, 7, "body__chp_1") == "body__chp_1__art_7"

    def test_bucket_eids_uniek_per_sectie(self):
        # Twee "Hoofdstuk 1"-en in verschillende secties botsen niet meer.
        a = _make_eid(1, "1", 0, _BUCKET_EID["Lichaam"])
        b = _make_eid(1, "1", 0, _BUCKET_EID["Toelichting"])
        assert a != b


class TestBuckets:
    def test_drie_buckets_in_volgorde(self):
        # De insert-volgorde gebruikt _BUCKETS.index — Lichaam moet eerst.
        assert _BUCKETS == ("Lichaam", "Bijlage", "Toelichting")

    def test_alle_buckets_hebben_eid(self):
        for b in _BUCKETS:
            assert b in _BUCKET_EID
            assert _BUCKET_EID[b].isalpha()
