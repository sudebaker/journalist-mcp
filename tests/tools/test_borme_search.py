#!/usr/bin/env python3
"""Offline unit tests for tools/borme_search/main.py — evidence contract.

No network access: exercises fetch-independent logic (matching, contract
shapes, empty results, raw preservation, deterministic ids).

Standalone runner: ``python3 tests/tools/test_borme_search.py``
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))

from borme_search import main as borme  # noqa: E402


def test_matches_target_nif_exact_and_name_substring():
    assert borme.matches_target({"nif": "B12345678"}, "b12345678", "nif")
    assert borme.matches_target({"nif": "B12345678"}, "B12345678", "nif")
    assert borme.matches_target({"nombre": "TEST SL SA"}, "Test Sl", "name")
    assert not borme.matches_target({"nif": "A98765432"}, "B12345678", "nif")


def test_search_builds_contract_evidence_with_raw():
    items = [{"nif": "B12345678", "nombre": "TEST SL",
              "actos": ["Nombramiento"], "url": "https://boe.es/y1"}]
    borme.fetch_borme_day = lambda d: items  # type: ignore[assignment]
    results, err = borme.search_borme("B12345678", "nif", "2025-01-01", "2025-01-01")
    assert err is None
    assert len(results) == 1
    ev = results[0]
    assert ev["source"] == "borme"
    assert ev["official"] is True
    assert ev["title"] == "TEST SL"
    assert ev["entity"] == "B12345678"
    assert ev["raw"]["nif"] == "B12345678"
    assert ev["raw"]["actos"] == ["Nombramiento"]
    assert len(ev["id"]) == 16


def test_empty_results_is_success_zero():
    borme.fetch_borme_day = lambda d: []  # type: ignore[assignment]
    results, err = borme.search_borme("NADIE", "name", "2025-01-01", "2025-01-01")
    assert err is None
    assert results == []
    env = borme.build_search_result("borme", "NADIE", results)
    assert env["count"] == 0 and env["results"] == []


def test_changed_field_missing_tolerated():
    # Formato cambiado: item sin 'nif'/'url' no debe romper el builder.
    borme.fetch_borme_day = lambda d: [{"nombre": "TEST SL"}]  # type: ignore[assignment]
    results, err = borme.search_borme("TEST SL", "name", "2025-01-01", "2025-01-01")
    assert err is None
    assert len(results) == 1
    assert results[0]["url"] == ""


def test_duplicate_ids_stable_across_calls():
    ev1 = borme.build_evidence("borme", True, 0.9, "T", "20250101",
                               "https://boe.es/y", entity="B1")
    ev2 = borme.build_evidence("borme", True, 0.9, "T", "20250101",
                               "https://boe.es/y", entity="B1")
    assert ev1["id"] == ev2["id"]


if __name__ == "__main__":
    tests = [
        test_matches_target_nif_exact_and_name_substring,
        test_search_builds_contract_evidence_with_raw,
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