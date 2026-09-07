#!/usr/bin/env python3
"""Offline unit tests for tools/borme_search/main.py — real nested sumario.

No network access: fixtures mirror the real API response shape
(data.sumario.diario[].seccion[], with section C items nested under
apartado[].item[]).

Standalone runner: ``python3 tests/tools/test_borme_search.py``
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))

from borme_search import main as borme  # noqa: E402


# Fixture: estructura real del sumario BORME
REAL_BORME_SUMARIO = {
    "status": {"code": "200", "text": "ok"},
    "data": {
        "sumario": {
            "diario": [
                {
                    "numero": "170",
                    "seccion": [
                        {
                            "codigo": "A",
                            "nombre": "SECCIÓN PRIMERA. Empresarios. Actos inscritos",
                            "item": [
                                {
                                    "identificador": "BORME-A-2026-170-01",
                                    "titulo": "ARABA/ÁLAVA",
                                    "url_html": "https://www.boe.es/diario_borme/txt.php?id=BORME-A-2026-170-01",
                                    "url_xml": "https://www.boe.es/diario_borme/xml.php?id=BORME-A-2026-170-01",
                                }
                            ],
                        },
                        {
                            "codigo": "C",
                            "nombre": "SECCIÓN SEGUNDA. Anuncios y avisos legales",
                            "apartado": [
                                {
                                    "codigo": "002",
                                    "nombre": "CONVOCATORIAS DE JUNTAS",
                                    "item": [
                                        {
                                            "identificador": "BORME-C-2026-4855",
                                            "titulo": "CONTRATAS Y OBRAS SAN GREGORIO, S.A.",
                                            "url_html": "https://www.boe.es/diario_borme/txt.php?id=BORME-C-2026-4855",
                                        }
                                    ],
                                }
                            ],
                        },
                    ],
                }
            ],
        }
    },
}


PROVINCE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<documento fecha_actualizacion="20260903T071717Z">
  <metadatos>
    <identificador>BORME-A-2026-170-01</identificador>
    <titulo>ARABA/ÁLAVA</titulo>
    <seccion codigo="A">SECCIÓN PRIMERA. Empresarios. Actos inscritos</seccion>
    <fecha_publicacion>20260903</fecha_publicacion>
  </metadatos>
  <texto>
    <p class="articulo">401698 - QUALIS CONSULTORES DE TALENTO SOCIEDAD LIMITADA.</p>
    <p class="parrafo">Declaración de unipersonalidad. Socio único: TRISKELION INVESTMENTS SL.</p>
    <p class="articulo">401699 - SEÑALIZACION Y BALIZAMIENTOS JUNDIZ, SOCIEDAD LIMITADA.</p>
    <p class="parrafo">Sociedad unipersonal. Cambio de identidad del socio único.</p>
  </texto>
</documento>
"""


def test_parse_province_xml_groups_companies_and_acts():
    companies = borme.parse_province_xml(PROVINCE_XML)
    assert len(companies) == 2
    assert companies[0]["name"] == "QUALIS CONSULTORES DE TALENTO SOCIEDAD LIMITADA"
    assert companies[0]["acts"] == ["Declaración de unipersonalidad. Socio único: TRISKELION INVESTMENTS SL."]
    assert companies[1]["name"] == "SEÑALIZACION Y BALIZAMIENTOS JUNDIZ, SOCIEDAD LIMITADA"
    assert companies[1]["acts"] == ["Sociedad unipersonal. Cambio de identidad del socio único."]


def test_parse_province_xml_invalid_returns_empty():
    assert borme.parse_province_xml("<not-xml") == []
    assert borme.parse_province_xml("") == []


def test_clean_company_name_strips_number_and_dot():
    assert borme._clean_company_name("401698 - QUALIS SOCIEDAD LIMITADA.") == "QUALIS SOCIEDAD LIMITADA"
    assert borme._clean_company_name("Sin numero.") == "Sin numero"


def test_iter_borme_items_marks_section_and_apartado():
    items = borme.iter_borme_items(REAL_BORME_SUMARIO)
    assert len(items) == 2
    assert items[0]["seccion"] == "A"
    assert items[0]["apartado"] == ""
    assert items[0]["titulo"] == "ARABA/ÁLAVA"
    assert items[1]["seccion"] == "C"
    assert items[1]["apartado"] == "CONVOCATORIAS DE JUNTAS"
    assert items[1]["titulo"] == "CONTRATAS Y OBRAS SAN GREGORIO, S.A."


def test_iter_borme_items_tolerates_empty():
    assert borme.iter_borme_items({}) == []


def test_name_matches_normalized_bidirectional():
    assert borme.name_matches("CONTRATAS Y OBRAS SAN GREGORIO", "Contratas y Obras San Gregorio, S.A.")
    assert borme.name_matches("Santander", "BANCO SANTANDER, S.A.")
    assert not borme.name_matches("BBVA", "BANCO SANTANDER, S.A.")


def test_default_lookback_days_env():
    import os as _os
    saved = _os.environ.get("BORME_LOOKBACK_DAYS")
    try:
        _os.environ.pop("BORME_LOOKBACK_DAYS", None)
        assert borme._default_lookback_days() == 7
        _os.environ["BORME_LOOKBACK_DAYS"] = "3"
        assert borme._default_lookback_days() == 3
        _os.environ["BORME_LOOKBACK_DAYS"] = "abc"
        assert borme._default_lookback_days() == 7
    finally:
        if saved is None:
            _os.environ.pop("BORME_LOOKBACK_DAYS", None)
        else:
            _os.environ["BORME_LOOKBACK_DAYS"] = saved


def test_empty_results_is_success_zero():
    borme.fetch_borme_day = lambda d: []  # type: ignore[assignment]
    results, err = borme.search_borme("NADIE", "name", "2025-01-01", "2025-01-01")
    assert err is None
    assert results == []
    env = borme.build_search_result("borme", "NADIE", results)
    assert env["count"] == 0 and env["results"] == []


def test_duplicate_ids_stable_across_calls():
    ev1 = borme.build_evidence("borme", True, 0.9, "T", "20250101",
                               "https://boe.es/y", entity="B1")
    ev2 = borme.build_evidence("borme", True, 0.9, "T", "20250101",
                               "https://boe.es/y", entity="B1")
    assert ev1["id"] == ev2["id"]


def test_search_borme_section_c_direct_match_no_xml(monkeypatch):
    items = borme.iter_borme_items(REAL_BORME_SUMARIO)
    borme.fetch_borme_day = lambda d: items  # type: ignore[assignment]
    monkeypatch.setattr(borme, "fetch_province_xml", lambda url: None)
    results, err = borme.search_borme(
        "SAN GREGORIO", "name", "2026-09-03", "2026-09-03", count=10
    )
    assert err is None
    assert len(results) == 1
    ev = results[0]
    assert ev["source"] == "borme"
    assert ev["title"] == "CONTRATAS Y OBRAS SAN GREGORIO, S.A."
    assert ev["metadata"]["seccion"] == "C"
    assert ev["metadata"]["apartado"] == "CONVOCATORIAS DE JUNTAS"
    assert ev["date"] == "2026-09-03"


def test_search_borme_section_a_uses_province_xml(monkeypatch):
    items = borme.iter_borme_items(REAL_BORME_SUMARIO)
    a_items = [it for it in items if it["seccion"] == "A"]
    borme.fetch_borme_day = lambda d: a_items  # type: ignore[assignment]
    monkeypatch.setattr(borme, "fetch_province_xml", lambda url: PROVINCE_XML)
    results, err = borme.search_borme(
        "QUALIS", "name", "2026-09-03", "2026-09-03", count=10
    )
    assert err is None
    assert len(results) == 1
    ev = results[0]
    assert ev["source"] == "borme"
    assert ev["title"] == "QUALIS CONSULTORES DE TALENTO SOCIEDAD LIMITADA"
    assert ev["metadata"]["seccion"] == "A"
    assert ev["metadata"]["provincia"] == "ARABA/ÁLAVA"
    assert ev["metadata"]["actos"] == ["Declaración de unipersonalidad. Socio único: TRISKELION INVESTMENTS SL."]
    assert ev["date"] == "2026-09-03"


def test_search_borme_rejects_nif():
    results, err = borme.search_borme("B12345678", "nif", "2026-09-03", "2026-09-03")
    assert results is None
    assert err is not None
    assert "NIF" in err


def test_search_borme_empty_success_zero():
    borme.fetch_borme_day = lambda d: []  # type: ignore[assignment]
    results, err = borme.search_borme("NADIE", "name", "2025-01-01", "2025-01-01")
    assert err is None
    assert results == []


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
