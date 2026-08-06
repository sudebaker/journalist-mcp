#!/usr/bin/env python3
"""Smoke tests for tools/transparency_search/main.py.

Standalone runner: ``python3 tests/tools/test_transparency_search.py`` (no
pytest infra required; pytest-compatible assertions included).

These tests invoke the tool via the subprocess protocol (stdin JSON -> stdout
JSON). The live case performs a real HTTP call to the Portal de Transparencia;
hits are not guaranteed because the portal layout may change, so we assert the
*pipeline* (success + valid response shape), not a specific count.
"""

import json
import os
import subprocess
import sys
from typing import Any

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
TOOL_DIR = os.path.normpath(os.path.join(ROOT, "tools", "transparency_search"))
PYTHON = sys.executable


def run_tool(arguments: dict[str, Any], request_id: str = "smoke") -> dict[str, Any]:
    """Invoke tools/transparency_search/main.py via stdin/stdout protocol."""
    payload = json.dumps({"request_id": request_id, "arguments": arguments})
    proc = subprocess.run(
        [PYTHON, "main.py"],
        input=payload,
        capture_output=True,
        text=True,
        cwd=TOOL_DIR,
        timeout=130,
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


def test_live_search_returns_valid_shape():
    """Búsqueda real: pipeline OK; hit-count no garantizado por layout del portal."""
    out = run_tool({
        "target": "Ministerio de Hacienda",
        "date_from": "2025-01-01",
        "date_to": "2026-01-01",
        "count": 3,
    })
    assert out["success"] is True, f"falló la búsqueda: {out.get('error')}"
    sc = out["structured_content"]
    assert sc["source"] == "TRANSPARENCIA"
    assert sc["official"] is True
    assert sc["confidence"] == 0.9
    assert sc.get("query") == "Ministerio de Hacienda"
    assert isinstance(sc["results"], list)
    assert sc["count"] == len(sc["results"])
    assert sc["count"] <= 3
    assert "retrieved_at" in sc
    if sc["results"]:
        ev = sc["results"][0]
        for field in ("id", "source", "official", "confidence", "title",
                      "date", "url", "metadata", "retrieved_at", "query"):
            assert field in ev, f"evidence missing field: {field}"
        assert ev["source"] == "TRANSPARENCIA"
    sys.stderr.write(
        f"\n[smoke] live OK: {sc['count']} results "
        f"(layout may legitimately yield 0 if portal markup changed).\n"
    )


def test_parser_extracts_semantic_rows_offline():
    """Valida TransparenciaParser sobre HTML real del portal (offline)."""
    sys.path.insert(0, os.path.normpath(os.path.join(ROOT, "tools")))
    from transparency_search.main import TransparenciaParser

    html = (
        '<html><body>'
        '<div class="div_busq">'
        '<p class="title_justif h4Size">'
        '<a href="/servicios-buscador/contenido/contratolicitacion.htm?id=Licitacion_123&amp;lang=es">'
        'Limpieza de edificios administrativos del Ministerio</a>'
        '</p>'
        '<p class="letra_grisacea h5Size">Contratos</p>'
        '<p class="letra_grisacea min_parr h6Size">'
        'Procedimiento Abierto. Adjudicado el 17-09-2015. Ministerio de Hacienda.</p>'
        '</div>'
        '<div class="otra-cosa">no relevante</div>'
        '</body></html>'
    )
    parser = TransparenciaParser()
    parser.feed(html)
    assert len(parser.results) == 1, parser.results
    r = parser.results[0]
    assert "Limpieza de edificios administrativos del Ministerio" in r.get("title", "")
    assert r.get("categoria") == "Contratos"
    assert "Adjudicado el 17-09-2015" in r.get("description", "")
    assert r.get("url", "").startswith("https://transparencia.gob.es/")


if __name__ == "__main__":
    tests = [
        test_missing_target_returns_error,
        test_live_search_returns_valid_shape,
        test_parser_extracts_semantic_rows_offline,
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
