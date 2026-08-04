#!/usr/bin/env python3
"""Smoke tests for tools/contratacion_search/main.py.

Standalone runner: ``python3 tests/tools/test_contratacion_search.py`` (no
pytest infra required; pytest-compatible assertions included).

These tests invoke the tool via the subprocess protocol (stdin JSON -> stdout
JSON). Live-feed cases perform real network calls to PLACSP; the
RANGE_TOO_WIDE, missing-target, invalid-JSON and parse/offline cases are offline.

Note on the live smoke test: the public PLACSP live ATOM feed only carries the
most recent profiles, and "Banco Santander" is not guaranteed to appear there.
We therefore assert the *pipeline* (success + valid response shape), not a
specific hit count, and bound the live crawl with CONTRATACION_MAX_PAGES so a
0-match run finishes in seconds instead of walking the full 20-page cap.
"""

import json
import os
import subprocess
import sys
import tempfile
import zipfile
from typing import Any

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
TOOL_DIR = os.path.normpath(os.path.join(ROOT, "tools", "contratacion_search"))
PYTHON = sys.executable


def run_tool(arguments: dict[str, Any], request_id: str = "smoke",
             env_extra: dict | None = None) -> dict[str, Any]:
    """Invoke tools/contratacion_search/main.py via stdin/stdout protocol."""
    payload = json.dumps({"request_id": request_id, "arguments": arguments})
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        [PYTHON, "main.py"],
        input=payload,
        capture_output=True,
        text=True,
        cwd=TOOL_DIR,
        timeout=130,
        env=env,
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


def test_invalid_json_returns_error():
    proc = subprocess.run(
        [PYTHON, "main.py"],
        input="not json",
        capture_output=True,
        text=True,
        cwd=TOOL_DIR,
        timeout=130,
    )
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["success"] is False
    assert out["error"]["code"] == "INVALID_JSON"


def test_zip_range_too_wide_is_rejected_without_download():
    """7 años (2020-2026) supera el techo de 5 años -> RANGE_TOO_WIDE (offline)."""
    out = run_tool({
        "target": "X",
        "target_type": "name",
        "date_from": "2020-01-01",
        "date_to": "2026-01-01",
        "count": 3,
    })
    assert out["success"] is False
    assert out["error"]["code"] == "SEARCH_FAILED"
    assert "RANGE_TOO_WIDE" in out["error"]["message"] or "máx 5" in out["error"]["message"]


def test_live_path_runs_and_returns_valid_shape():
    """Rango reciente (<=90 días -> live atom). Pipeline OK; hit-count no garantizado."""
    out = run_tool({
        "target": "Banco Santander",
        "target_type": "name",
        "date_from": "2026-06-01",
        "date_to": "2026-08-03",
        "count": 3,
    }, env_extra={"CONTRATACION_MAX_PAGES": "3"})
    assert out["success"] is True, f"falló la búsqueda: {out.get('error')}"
    sc = out["structured_content"]
    assert sc["source"] == "CONTRATACION"
    assert sc["official"] is True
    assert sc["confidence"] == 1.0
    assert sc.get("query") == "name:Banco Santander"
    assert isinstance(sc["results"], list)
    assert sc["count"] == len(sc["results"])
    assert sc["count"] <= 3
    assert sc["date_from"] == "2026-06-01"
    assert sc["date_to"] == "2026-08-03"
    assert "retrieved_at" in sc
    sys.stderr.write(
        f"\n[smoke] live OK: {sc['count']} results "
        f"(target may legitimately be 0 in the live feed).\n"
    )


def test_parse_entry_extracts_codice_fields_offline():
    """Valida _parse_entry sobre un entry CODICE de ejemplo (offline)."""
    sys.path.insert(0, os.path.normpath(os.path.join(ROOT, "tools")))
    from contratacion_search.main import _parse_entry, _entry_matches, NS
    import xml.etree.ElementTree as ET

    feed = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom"'
        ' xmlns:cac="urn:dgpe:names:draft:codice:schema:xsd:CommonAggregateComponents-2"'
        ' xmlns:cac-place-ext="urn:dgpe:names:draft:codice-place-ext:schema:xsd:CommonAggregateComponents-2"'
        ' xmlns:cbc="urn:dgpe:names:draft:codice:schema:xsd:CommonBasicComponents-2"'
        ' xmlns:cbc-place-ext="urn:dgpe:names:draft:codice-place-ext:schema:xsd:CommonBasicComponents-2">'
        '<entry>'
        '<title>Obra de acceso público en el Puerto de las Sombras</title>'
        '<updated>2026-07-28T10:00:00.000+02:00</updated>'
        '<summary>Id licitación: 1/2026; Importe: 5000 EUR; Estado: ADJ</summary>'
        '<link href="https://contrataciondelestado.es/detalle/1"/>'
        '<cac-place-ext:ContractFolderStatus>'
        '<cbc:ContractFolderID>1/2026</cbc:ContractFolderID>'
        '<cbc-place-ext:ContractFolderStatusCode languageID="es">ADJ</cbc-place-ext:ContractFolderStatusCode>'
        '<cac-place-ext:LocatedContractingParty><cac:Party>'
        '<cac:PartyName><cbc:Name>AYUNTAMIENTO DE LAS SOMBRAS</cbc:Name></cac:PartyName>'
        '<cbc:PartyIdentification><cbc:ID schemeName="NIF">Q1234567L</cbc:ID></cbc:PartyIdentification>'
        '</cac:Party></cac-place-ext:LocatedContractingParty>'
        '<cac:ProcurementProject><cac:BudgetAmount>'
        '<cbc:EstimatedOverallContractAmount currencyID="EUR">5000.00</cbc:EstimatedOverallContractAmount>'
        '</cac:BudgetAmount></cac:ProcurementProject>'
        '<cac:WinningParty>'
        '<cac:PartyName><cbc:Name>MARISCOS Y CRUSTACEOS SLU</cbc:Name></cac:PartyName>'
        '<cbc:PartyIdentification><cbc:ID schemeName="NIF">B70986187</cbc:ID></cbc:PartyIdentification>'
        '</cac:WinningParty>'
        '</cac-place-ext:ContractFolderStatus>'
        '</entry></feed>'
    )

    root = ET.fromstring(feed)
    entry = root.find("atom:entry", NS)
    ev = _parse_entry(entry, "name:Mariscos")
    md = ev["metadata"]
    assert ev["source"] == "CONTRATACION"
    assert ev["official"] is True
    assert ev["confidence"] == 1.0
    assert ev["title"] == "Obra de acceso público en el Puerto de las Sombras"
    assert ev["date"] == "2026-07-28"
    assert md["id_licitacion"] == "1/2026"
    assert md["estado"] == "ADJ"
    assert md["organo_contratacion"] == "AYUNTAMIENTO DE LAS SOMBRAS"
    assert md["adjudicatario_nombre"] == "MARISCOS Y CRUSTACEOS SLU"
    assert md["importe_estimado"] == "5000.00"
    assert "B70986187" in md["nifs"]
    # matching por nombre y por NIF
    assert _entry_matches(ev, "Mariscos y Crustaceos", "name")
    assert _entry_matches(ev, "b70986187", "nif")
    assert not _entry_matches(ev, "99999999Z", "nif")


if __name__ == "__main__":
    tests = [
        test_missing_target_returns_error,
        test_invalid_json_returns_error,
        test_zip_range_too_wide_is_rejected_without_download,
        test_live_path_runs_and_returns_valid_shape,
        test_parse_entry_extracts_codice_fields_offline,
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
