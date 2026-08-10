#!/usr/bin/env python3
"""Tests for tools/common/evidence.py.

Standalone runner: ``python3 tests/common/test_evidence.py`` (no pytest
infra required; pytest-compatible assertions included).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))

from common.evidence import (  # noqa: E402
    build_evidence,
    build_search_result,
)


def test_evidence_structure():
    ev = build_evidence(
        source="Example News",
        official=True,
        confidence=0.95,
        title="Breaking: Test Title",
        date="2025-01-01T10:00:00Z",
        url="https://example.com/news/1",
        metadata={"author": "Jane Doe"},
        query="test query",
    )
    expected_fields = {
        "id",
        "source",
        "official",
        "confidence",
        "title",
        "date",
        "url",
        "metadata",
        "retrieved_at",
        "query",
    }
    assert set(ev.keys()) == expected_fields, ev.keys()
    assert ev["source"] == "Example News"
    assert ev["official"] is True
    assert ev["confidence"] == 0.95
    assert ev["title"] == "Breaking: Test Title"
    assert ev["date"] == "2025-01-01T10:00:00Z"
    assert ev["url"] == "https://example.com/news/1"
    assert ev["metadata"] == {"author": "Jane Doe"}
    assert ev["query"] == "test query"
    assert ev["id"] and len(ev["id"]) == 16
    assert "T" in ev["retrieved_at"] and ev["retrieved_at"].endswith("+00:00")


def test_evidence_optional_contract_fields():
    ev = build_evidence(
        source="bdns",
        official=True,
        confidence=1.0,
        title="Concesión",
        date="2025-01-01",
        url="https://pap.hacienda.gob.es/x",
        metadata={"codConcesion": "X-1"},
        query="q",
        description="Ayuda directa",
        entity="B12345678",
        evidence_type="official_record",
        raw={"importe": 1000},
    )
    assert ev["description"] == "Ayuda directa"
    assert ev["entity"] == "B12345678"
    assert ev["evidence_type"] == "official_record"
    assert ev["raw"] == {"importe": 1000}
    # metadata is preserved alongside the new contract fields
    assert ev["metadata"] == {"codConcesion": "X-1"}


def test_evidence_id_determinism():
    import hashlib
    ev1 = build_evidence("S", False, 0.5, "Title", "2025-01-01",
                         "http://x", {}, "q")
    ev2 = build_evidence("S", False, 0.5, "Title", "2025-01-01",
                         "http://x", {}, "q")
    assert ev1["id"] == ev2["id"]
    expected = hashlib.sha256(b"http://x|2025-01-01|Title").hexdigest()[:16]
    assert ev1["id"] == expected


def test_evidence_id_differs_on_url():
    ev_a = build_evidence("S", False, 0.5, "T", "d", "http://a", {}, "")
    ev_b = build_evidence("S", False, 0.5, "T", "d", "http://b", {}, "")
    assert ev_a["id"] != ev_b["id"]


def test_search_result_envelope():
    ev = build_evidence("boe", True, 0.9, "T", "d", "http://x", {}, "q")
    result = build_search_result("boe", "B12345678", [ev], duration_ms=12)
    assert result["source"] == "boe"
    assert result["target"] == "B12345678"
    assert result["count"] == 1
    assert result["official"] is True
    assert result["evidence_type"] == "official_record"
    assert result["duration_ms"] == 12
    assert result["results"][0]["id"] == ev["id"]


def test_search_result_empty_no_match():
    result = build_search_result("boe", "B12345678", [])
    assert result["results"] == []
    assert result["count"] == 0
    # duration_ms omitted when not provided
    assert "duration_ms" not in result


if __name__ == "__main__":
    import hashlib

    expected_id = hashlib.sha256(
        b"http://x|2025-01-01|Title"
    ).hexdigest()[:16]
    print(f"deterministic id for 'http://x|2025-01-01|Title': {expected_id}",
          file=sys.stderr)

    tests = [
        test_evidence_structure,
        test_evidence_optional_contract_fields,
        test_evidence_id_determinism,
        test_evidence_id_differs_on_url,
        test_search_result_envelope,
        test_search_result_empty_no_match,
    ]
    passed = 0
    failed = 0
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