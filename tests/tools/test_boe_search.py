#!/usr/bin/env python3
"""Offline unit tests for tools/boe_search/main.py — real nested sumario.

No network access: fixtures mirror the real API response shape
(data.sumario.diario[].seccion[].departamento[].epigrafe[].item[]).

Standalone runner: ``python3 tests/tools/test_boe_search.py``
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))

from boe_search import main as boe  # noqa: E402

# Real function captured before other tests swap boe.fetch_boe_day for stubs.
_REAL_FETCH_BOE_DAY = boe.fetch_boe_day

# Fixture: estructura real observada en https://www.boe.es/datosabiertos/api/boe/sumario/{aaaammdd}
REAL_SUMARIO = {
    "status": {"code": "200", "text": "ok"},
    "data": {
        "sumario": {
            "metadatos": {"publicacion": "BOE", "fecha_publicacion": "20260903"},
            "diario": [
                {
                    "numero": "217",
                    "seccion": [
                        {
                            "codigo": "1",
                            "nombre": "I. Disposiciones generales",
                            "departamento": [
                                {
                                    "codigo": "9999",
                                    "nombre": "MINISTERIO DE PRUEBA",
                                    "epigrafe": [
                                        {
                                            "nombre": "Leyes Orgánicas",
                                            "item": [
                                                {
                                                    "identificador": "BOE-A-2026-11111",
                                                    "control": "2026/10001",
                                                    "titulo": "Ley 1/2026 de prueba con mención a TEST SL.",
                                                    "url_pdf": {"texto": "https://www.boe.es/boe/dias/2026/09/03/pdfs/BOE-A-2026-11111.pdf"},
                                                    "url_html": "https://www.boe.es/diario_boe/txt.php?id=BOE-A-2026-11111",
                                                    "url_xml": "https://www.boe.es/diario_boe/xml.php?id=BOE-A-2026-11111",
                                                }
                                            ],
                                        }
                                    ],
                                }
                            ],
                        },
                        {
                            "codigo": "2A",
                            "nombre": "II. Autoridades y personal. - A. Nombramientos",
                            "departamento": [
                                {
                                    "codigo": "1820",
                                    "nombre": "CONSEJO GENERAL DEL PODER JUDICIAL",
                                    "epigrafe": [
                                        {
                                            "nombre": "Situaciones",
                                            "item": {
                                                "identificador": "BOE-A-2026-22222",
                                                "titulo": "Acuerdo sobre el NIF B12345678.",
                                                "url_pdf": {"texto": "https://www.boe.es/boe/dias/2026/09/03/pdfs/BOE-A-2026-22222.pdf"},
                                            },
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


def test_date_range_bounds():
    dates = boe.date_range("2025-01-01", "2025-01-03")
    assert dates == ["20250101", "20250102", "20250103"]


def test_iter_boe_items_flattens_nested_sections():
    items = boe.iter_boe_items(REAL_SUMARIO)
    assert len(items) == 2
    assert items[0]["identificador"] == "BOE-A-2026-11111"
    assert items[0]["seccion"] == "1"
    assert items[0]["epigrafe"] == "Leyes Orgánicas"
    assert items[1]["identificador"] == "BOE-A-2026-22222"
    assert items[1]["seccion"] == "2A"
    assert items[1]["epigrafe"] == "Situaciones"


def test_iter_boe_items_tolerates_item_as_dict_and_empty():
    assert boe.iter_boe_items({}) == []
    assert boe.iter_boe_items({"data": {"sumario": {"diario": []}}}) == []


def test_matches_target_titulo_only():
    assert boe.matches_target({"titulo": "Acuerdo con B12345678"}, "b12345678", "nif")
    assert boe.matches_target({"titulo": "Ley mención a Test Sl"}, "test sl", "name")
    assert not boe.matches_target({"titulo": "Otra cosa"}, "B12345678", "nif")
    assert not boe.matches_target({"titulo": ""}, "X", "name")


def test_canonical_url_prefers_html_falls_back_pdf():
    item = {"url_html": "https://html", "url_pdf": {"texto": "https://pdf"}}
    assert boe.canonical_item_url(item) == "https://html"
    assert boe.canonical_item_url({"url_pdf": {"texto": "https://pdf"}}) == "https://pdf"
    assert boe.canonical_item_url({}) == ""


def test_search_boe_builds_contract_evidence_iso_date():
    # fetch devuelve items ya aplanados (como hará fetch_boe_day real)
    items = boe.iter_boe_items(REAL_SUMARIO)
    boe.fetch_boe_day = lambda d: items  # type: ignore[assignment]
    results, err = boe.search_boe("test sl", "name", "2026-09-03", "2026-09-03")
    assert err is None
    assert len(results) == 1
    ev = results[0]
    assert ev["source"] == "boe"
    assert ev["official"] is True
    assert ev["title"] == "Ley 1/2026 de prueba con mención a TEST SL."
    assert ev["date"] == "2026-09-03"
    assert ev["url"] == "https://www.boe.es/diario_boe/txt.php?id=BOE-A-2026-11111"
    assert ev["metadata"]["seccion"] == "1"
    assert ev["metadata"]["epigrafe"] == "Leyes Orgánicas"
    assert len(ev["id"]) == 16


def test_search_boe_nif_matches_uppercase_in_titulo():
    items = boe.iter_boe_items(REAL_SUMARIO)
    boe.fetch_boe_day = lambda d: items  # type: ignore[assignment]
    results, err = boe.search_boe("b12345678", "nif", "2026-09-03", "2026-09-03")
    assert err is None
    assert len(results) == 1
    assert results[0]["metadata"]["identificador"] == "BOE-A-2026-22222"


def test_empty_results_is_success_zero():
    boe.fetch_boe_day = lambda d: []  # type: ignore[assignment]
    results, err = boe.search_boe("NADIE", "name", "2025-01-01", "2025-01-01")
    assert err is None
    assert results == []
    env = boe.build_search_result("boe", "NADIE", results)
    assert env["count"] == 0 and env["results"] == []


class _FakeResp:
    status_code = 200

    def json(self):
        return REAL_SUMARIO


class _FakeResp500:
    status_code = 500

    def json(self):
        return {}


def test_fetch_boe_day_parses_real_nested_json():
    prev_fetch, prev_rr = boe.fetch_boe_day, boe.request_with_retry
    try:
        boe.fetch_boe_day = _REAL_FETCH_BOE_DAY
        boe.request_with_retry = lambda *a, **k: _FakeResp()
        items = boe.fetch_boe_day("20260903")
    finally:
        boe.fetch_boe_day = prev_fetch
        boe.request_with_retry = prev_rr
    assert len(items) == 2
    assert items[0]["identificador"] == "BOE-A-2026-11111"
    assert items[0]["seccion"] == "1"
    assert items[0]["epigrafe"] == "Leyes Orgánicas"
    assert items[1]["identificador"] == "BOE-A-2026-22222"
    assert items[1]["seccion"] == "2A"
    assert items[1]["epigrafe"] == "Situaciones"


def test_fetch_boe_day_non_200_returns_empty():
    prev_fetch, prev_rr = boe.fetch_boe_day, boe.request_with_retry
    try:
        boe.fetch_boe_day = _REAL_FETCH_BOE_DAY
        boe.request_with_retry = lambda *a, **k: _FakeResp500()
        items = boe.fetch_boe_day("20260903")
    finally:
        boe.fetch_boe_day = prev_fetch
        boe.request_with_retry = prev_rr
    assert items == []


def test_duplicate_ids_stable_across_calls():
    ev1 = boe.build_evidence("boe", True, 0.9, "T", "2026-09-03",
                             "https://boe.es/x", entity="B1")
    ev2 = boe.build_evidence("boe", True, 0.9, "T", "2026-09-03",
                             "https://boe.es/x", entity="B1")
    assert ev1["id"] == ev2["id"]


if __name__ == "__main__":
    tests = [
        test_date_range_bounds,
        test_iter_boe_items_flattens_nested_sections,
        test_iter_boe_items_tolerates_item_as_dict_and_empty,
        test_matches_target_titulo_only,
        test_canonical_url_prefers_html_falls_back_pdf,
        test_search_boe_builds_contract_evidence_iso_date,
        test_search_boe_nif_matches_uppercase_in_titulo,
        test_empty_results_is_success_zero,
        test_fetch_boe_day_parses_real_nested_json,
        test_fetch_boe_day_non_200_returns_empty,
        test_duplicate_ids_stable_across_calls,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}", file=sys.stderr)
            passed += 1
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"FAIL {t.__name__}: {e}", file=sys.stderr)
            failed += 1
    print(f"\n{passed} passed, {failed} failed", file=sys.stderr)
    sys.exit(1 if failed else 0)
