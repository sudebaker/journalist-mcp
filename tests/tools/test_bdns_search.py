#!/usr/bin/env python3
"""Smoke tests for tools/bdns_search/main.py.

Standalone runner: ``python3 tests/tools/test_bdns_search.py`` (no pytest
infra required; pytest-compatible assertions included).

These tests invoke the real BDNS REST API via the subprocess protocol
(stdin JSON -> stdout JSON). They perform live network calls, so they are
smoke tests, not unit tests.

The subprocess contract exercised here:
  stdin:  {"request_id":"...","arguments":{...}}
  stdout: single JSON {"success":bool,"request_id":...,"content":[...],
                       "structured_content":{...}}  OR  {"success":false,...,"error":{...}}
"""

import json
import os
import subprocess
import sys
from typing import Any

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
TOOL_DIR = os.path.normpath(os.path.join(ROOT, "tools", "bdns_search"))
PYTHON = sys.executable


def run_tool(arguments: dict[str, Any], request_id: str = "smoke") -> dict[str, Any]:
    """Invoke tools/bdns_search/main.py via stdin/stdout protocol."""
    payload = json.dumps({"request_id": request_id, "arguments": arguments})
    proc = subprocess.run(
        [PYTHON, "main.py"],
        input=payload,
        capture_output=True,
        text=True,
        cwd=TOOL_DIR,
        timeout=120,
    )
    assert proc.returncode == 0, (
        f"main.py exited {proc.returncode}. stderr:\n{proc.stderr[-2000:]}"
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"stdout no es JSON válido: {exc}\nstdout={proc.stdout[:1000]}\n"
            f"stderr={proc.stderr[-1000:]}"
        ) from exc


def test_missing_target_returns_error():
    """Sin target: error controlado con código MISSING_TARGET."""
    out = run_tool({"target": ""})
    assert out["success"] is False
    assert out["error"]["code"] == "MISSING_TARGET"


def test_name_search_returns_real_data():
    """target='Banco Santander', name: debe devolver success:true y count>0."""
    out = run_tool({
        "target": "Banco Santander",
        "target_type": "name",
        "date_from": "2025-01-01",
        "date_to": "2026-01-01",
        "count": 3,
    })
    assert out["success"] is True, f"falló la búsqueda: {out.get('error')}"
    assert out["request_id"] == "smoke"
    sc = out["structured_content"]
    assert sc["source"] == "BDNS"
    assert sc["official"] is True
    assert sc["confidence"] == 1.0
    assert sc["count"] > 0, "se esperaban resultados reales (count>0)"
    assert sc["date_from"] == "2025-01-01"
    assert sc["date_to"] == "2026-01-01"
    assert sc["query"] == "name:Banco Santander"
    assert sc["endpoint"] == "https://www.pap.hacienda.gob.es/bdnstrans/api/concesiones/busqueda"
    assert "retrieved_at" in sc
    assert len(sc["results"]) == sc["count"] <= 3
    ev0 = sc["results"][0]
    for field in ("id", "source", "official", "confidence", "title", "date",
                  "url", "metadata", "retrieved_at", "query"):
        assert field in ev0, f"evidence faltando campo {field}"
    assert ev0["source"] == "BDNS"
    assert ev0["official"] is True
    assert ev0["confidence"] == 1.0
    # text content in Spanish
    txt = out["content"][0]["text"]
    assert "BDNS" in txt
    sys.stderr.write(
        f"\n[smoke] name search OK: {sc['count']} results. "
        f"sample title: {ev0['title'][:60]}\n"
    )


def test_nif_search_returns_exact_match():
    """target='A39000013' (Banco Santander S.A.), nif: beneficiario exacto."""
    out = run_tool({
        "target": "A39000013",
        "target_type": "nif",
        "date_from": "2025-01-01",
        "date_to": "2026-12-31",
        "count": 3,
    })
    assert out["success"] is True, f"falló la búsqueda: {out.get('error')}"
    sc = out["structured_content"]
    assert sc["count"] > 0
    # nifCif devuelve concesiones con exactamente ese NIF en el beneficiario
    for ev in sc["results"]:
        assert "A39000013" in ev["title"]


def test_empty_result_is_success_zero():
    """Un NIF que no existe debe dar success:true con count:0 (no error)."""
    out = run_tool({
        "target": "Z99999999Z",
        "target_type": "nif",
        "date_from": "2025-01-01",
        "date_to": "2026-01-01",
        "count": 5,
    })
    assert out["success"] is True
    assert out["structured_content"]["count"] == 0


if __name__ == "__main__":
    tests = [
        test_missing_target_returns_error,
        test_name_search_returns_real_data,
        test_nif_search_returns_exact_match,
        test_empty_result_is_success_zero,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}", file=sys.stderr)
            passed += 1
        except Exception as exc:
            import traceback
            traceback.print_exc()
            print(f"FAIL {t.__name__}: {exc}", file=sys.stderr)
            failed += 1
    print(f"\n{passed} passed, {failed} failed", file=sys.stderr)
    sys.exit(1 if failed else 0)
