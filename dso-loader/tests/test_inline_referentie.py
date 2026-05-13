"""Unit-tests voor inline_referentie parser (geen DB-rondje)."""

from src.loaders.inline_referentie import extract_inline_referenties


class TestExtractInlineReferenties:
    def test_geen_inhoud(self):
        assert extract_inline_referenties(None) == []
        assert extract_inline_referenties("") == []

    def test_geen_referenties(self):
        inhoud = "<Al>Een gewoon stuk tekst zonder verwijzingen.</Al>"
        assert extract_inline_referenties(inhoud) == []

    def test_intioref_double_quotes(self):
        inhoud = '<Al>Zie de <IntIoRef ref="/join/id/regdata/gm0297/2024/GIO_001">kaart</IntIoRef>.</Al>'
        result = extract_inline_referenties(inhoud)
        assert len(result) == 1
        assert result[0]["soort"] == "IntIoRef"
        assert result[0]["target_ref"] == "/join/id/regdata/gm0297/2024/GIO_001"
        assert result[0]["positie"] > 0
        assert result[0]["eigen_wid"] is None  # alleen ExtIoRef heeft eigen_wid

    def test_intioref_single_quotes(self):
        inhoud = "<Al>Zie de <IntIoRef ref='/foo/bar'>kaart</IntIoRef>.</Al>"
        result = extract_inline_referenties(inhoud)
        assert len(result) == 1
        assert result[0]["target_ref"] == "/foo/bar"

    def test_alle_vier_soorten(self):
        inhoud = (
            '<Al>Zie <IntIoRef ref="/g/1">a</IntIoRef> '
            '<ExtIoRef ref="https://example.com/b">b</ExtIoRef> '
            '<IntRef ref="art_3">c</IntRef> '
            '<ExtRef ref="https://example.com/d">d</ExtRef></Al>'
        )
        result = extract_inline_referenties(inhoud)
        soorten = sorted(r["soort"] for r in result)
        assert soorten == ["ExtIoRef", "ExtRef", "IntIoRef", "IntRef"]

    def test_meerdere_zelfde_soort(self):
        inhoud = (
            '<Al><IntIoRef ref="/g/1">a</IntIoRef> en '
            '<IntIoRef ref="/g/2">b</IntIoRef></Al>'
        )
        result = extract_inline_referenties(inhoud)
        assert len(result) == 2
        assert {r["target_ref"] for r in result} == {"/g/1", "/g/2"}
        assert result[0]["positie"] != result[1]["positie"]

    def test_attribuut_volgorde_doet_niet_terzake(self):
        inhoud = '<Al><IntIoRef xmlns="x" id="abc" ref="/g/1" class="y">k</IntIoRef></Al>'
        result = extract_inline_referenties(inhoud)
        assert len(result) == 1
        assert result[0]["target_ref"] == "/g/1"

    def test_lege_ref_overgeslagen(self):
        inhoud = '<Al><IntIoRef ref="">x</IntIoRef></Al>'
        result = extract_inline_referenties(inhoud)
        assert result == []

    def test_case_sensitivity_van_tagname(self):
        # STOP-tag-namen zijn altijd PascalCase; we matchen case-insensitive
        # om robuust te zijn bij eventuele casing-verschuivingen.
        inhoud = '<Al><intioref ref="/g/1">k</intioref></Al>'
        result = extract_inline_referenties(inhoud)
        assert len(result) == 1
        assert result[0]["soort"].lower() == "intioref"

    def test_positie_klopt_met_offset(self):
        prefix = "<Al>Zie hieronder: "
        inhoud = prefix + '<IntIoRef ref="/g/1">k</IntIoRef></Al>'
        result = extract_inline_referenties(inhoud)
        assert len(result) == 1
        # positie wijst naar het '<' van de tag-opening
        assert inhoud[result[0]["positie"]] == "<"
        assert inhoud[result[0]["positie"]:].startswith("<IntIoRef")

    def test_extioref_eigen_wid_wordt_opgeslagen(self):
        # ExtIoRef heeft @wId zodat IntIoRef'en hiernaar kunnen wijzen.
        inhoud = (
            '<Al><ExtIoRef wId="gm1586_502fc5cd23844cb18d5ed48ab9cbe853__cmp_o_1__ref_o_1" '
            'eId="cmp_o_1__ref_o_1" '
            'ref="/join/id/regdata/gm1586/2024/abc/nld@2024-12-18;1">'
            '/join/id/regdata/gm1586/2024/abc/nld@2024-12-18;1'
            "</ExtIoRef></Al>"
        )
        result = extract_inline_referenties(inhoud)
        assert len(result) == 1
        assert result[0]["soort"] == "ExtIoRef"
        assert result[0]["target_ref"].startswith("/join/id/regdata/")
        assert result[0]["eigen_wid"] == "gm1586_502fc5cd23844cb18d5ed48ab9cbe853__cmp_o_1__ref_o_1"

    def test_intioref_naar_extioref_keten_eigen_wid_blijft_none(self):
        # IntIoRef heeft alleen @ref naar de wId van een ExtIoRef; eigen_wid blijft None.
        inhoud = '<Al><IntIoRef ref="gm1586_502fc5cd__cmp_o_1__ref_o_1">kaart 1</IntIoRef></Al>'
        result = extract_inline_referenties(inhoud)
        assert len(result) == 1
        assert result[0]["eigen_wid"] is None
        assert result[0]["target_ref"] == "gm1586_502fc5cd__cmp_o_1__ref_o_1"

    def test_extioref_zonder_wid_eigen_wid_is_none(self):
        inhoud = '<Al><ExtIoRef ref="/join/id/regdata/gm/2024/x">x</ExtIoRef></Al>'
        result = extract_inline_referenties(inhoud)
        assert len(result) == 1
        assert result[0]["soort"] == "ExtIoRef"
        assert result[0]["eigen_wid"] is None  # ExtIoRef zonder wId mogelijk
