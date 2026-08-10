#!/usr/bin/env python3
"""Offline unit tests for tools/ted_search/main.py — evidence contract.

No network access: exercises fetch-independent logic (query building,
contract shapes, empty results, error classification, deterministic ids).

Standalone runner: ``python3 tests/tools/test_ted_search.py``
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))

from ted_search import main as ted  # noqa: E402


def test_build_expert_query_nif_and_name():
    q = ted.build_expert_query("B12345678", "nif", "20250101", "20251231")
    assert 'organisation-identifier-buyer="B12345678"' in q
    assert "PD>=20250101" in q and "PD<=20251231" in q
    q2 = ted.build_expert_query("Test SL", "name", "", "")
    assert 'organisation-name-buyer="Test SL"' in q2


def _stub_response(status_code: int, payload: dict) -> object:
    class R:
        json = staticmethod(lambda: payload)

    R.status_code = status_code
    return R()


def test_search_builds_contract_evidence():
    notices = [{
        "ND": "123-2025",
        "notice-title": "Suministro",
        "publication-date": "2025-06-01",
        "organisation-name-buyer": "Ministerio",
        "organisation-identifier-buyer": "S2800116D",
    }]
    ted.request_with_retry = lambda *a, **k: _stub_response(200, {"notices": notices})  # type: ignore[assignment]
    results, err = ted.search_ted("S2800116D", "nif", "", "", 10)
    assert err is None
    assert len(results) == 1
    ev = results[0]
    assert ev["source"] == "ted"
    assert ev["official"] is True
    assert ev["title"] == "Suministro"
    assert ev["date"] == "2025-06-01"
    assert ev["entity"] == "S2800116D"
    assert ev["raw"]["notice_id"] == "123-2025"
    assert ev["url"] == "https://ted.europa.eu/en/notice/-/detail/123-2025"
    assert len(ev["id"]) == 16


def test_empty_results_is_success_zero():
    ted.request_with_retry = lambda *a, **k: _stub_response(200, {"notices": []})  # type: ignore[assignment]
    results, err = ted.search_ted("X", "nif", "", "", 10)
    assert err is None
    assert results == []


def test_http_error_returns_search_failed():
    ted.request_with_retry = lambda *a, **k: _stub_response(503, {})  # type: ignore[assignment]
    results, err = ted.search_ted("X", "nif", "", "", 10)
    assert results is None
    assert err is not None and "503" in err


def test_timeout_classified():
    class TimeoutLike(Exception):
        pass

    def boom(*a, **k):
        raise TimeoutError("timed out")
    ted.request_with_retry = boom  # type: ignore[assignment]
    results, err = ted.search_ted("X", "nif", "", "", 10)
    assert results is None
    assert "timed out" in (err or "").lower()


def test_duplicate_ids_stable_across_calls():
    ev1 = ted.build_evidence("ted", True, 0.9, "T", "d",
                             "https://ted.europa.eu/x", entity="B1")
    ev2 = ted.build_evidence("ted", True, 0.9, "T", "d",
                             "https://ted.europa.eu/x", entity="B1")
    assert ev1["id"] == ev2["id"]


if __name__ == "__main__":
    tests = [
        test_build_expert_query_nif_and_name,
        test_search_builds_contract_evidence,
        test_empty_results_is_success_zero,
        test_http_error_returns_search_failed,
        test_timeout_classified,
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