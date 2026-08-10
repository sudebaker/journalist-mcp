#!/usr/bin/env python3
"""Offline unit tests for tools/boe_search/main.py — evidence contract.

No network access: exercises fetch-independent logic (matching, contract
shapes, empty results, changed-field tolerance, deterministic ids).

Standalone runner: ``python3 tests/tools/test_boe_search.py``
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))

from boe_search import main as boe  # noqa: E402


def test_date_range_bounds():
    dates = boe.date_range("2025-01-01", "2025-01-03")
    assert dates == ["20250101", "20250102", "20250103"]


def test_matches_target_nif_upper_and_name():
    assert boe.matches_target({"titulo": "Anuncio B12345678"}, "B12345678", "nif")
    assert boe.matches_target({"titulo": "Anuncio b12345678"}, "B12345678", "nif")
    assert boe.matches_target({"contenido": "texto con TEST SL"}, "Test SL", "name")
    assert not boe.matches_target({"titulo": "Otro anuncio"}, "B12345678", "nif")


def test_search_builds_contract_evidence():
    items = [
        {"titulo": "Anuncio TEST SL", "url": "https://boe.es/a1",
         "contenido": "Contenido del anuncio TEST SL"},
        {"titulo": "Otro anuncio", "url": "https://boe.es/a2", "contenido": ""},
    ]
    boe.fetch_boe_day = lambda d: items  # type: ignore[assignment]
    results, err = boe.search_boe("TEST SL", "name", "2025-01-01", "2025-01-01")
    assert err is None
    assert len(results) == 1
    ev = results[0]
    assert ev["source"] == "boe"
    assert ev["official"] is True
    assert ev["title"] == "Anuncio TEST SL"
    assert ev["entity"] == "TEST SL"
    assert ev["date"] == "20250101"
    assert len(ev["id"]) == 16


def test_empty_results_is_success_zero():
    boe.fetch_boe_day = lambda d: []  # type: ignore[assignment]
    results, err = boe.search_boe("NADIE", "name", "2025-01-01", "2025-01-01")
    assert err is None
    assert results == []
    env = boe.build_search_result("boe", "NADIE", results)
    assert env["count"] == 0 and env["results"] == []


def test_changed_field_missing_tolerated():
    # Formato cambiado: item sin 'contenido' no debe romper el builder.
    boe.fetch_boe_day = lambda d: [{"title": "Nuevo nombre de campo"}]  # type: ignore[assignment]
    results, err = boe.search_boe("X", "nif", "2025-01-01", "2025-01-01")
    assert err is None
    # Sin 'titulo'/url original no matchea el target X en los campos conocidos.
    assert results == []


def test_duplicate_ids_stable_across_calls():
    ev1 = boe.build_evidence("boe", True, 0.9, "T", "20250101",
                             "https://boe.es/x", entity="B1")
    ev2 = boe.build_evidence("boe", True, 0.9, "T", "20250101",
                             "https://boe.es/x", entity="B1")
    assert ev1["id"] == ev2["id"]


if __name__ == "__main__":
    tests = [
        test_date_range_bounds,
        test_matches_target_nif_upper_and_name,
        test_search_builds_contract_evidence,
        test_empty_results_is_success_zero,
        test_changed_field_missing_tolerated,
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