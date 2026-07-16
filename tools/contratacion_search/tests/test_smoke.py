#!/usr/bin/env python3
"""Smoke tests for tools/contratacion_search/main.py.

These tests do NOT touch the network. They exercise:

* Argument validation (target required, target_type coerced, count bounded).
* JSON request/response contract (SubprocessResponse shape).
* HTML parser against a synthetic result table.
* JS payload construction (valid JS, target is JSON-escaped, no injection).
* Output markdown rendering (format_amount, format_record_line).

Run with: python3 tools/contratacion_search/tests/test_smoke.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

TOOL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOL_DIR))

# Import the tool directly so we can call internals without spawning a
# subprocess.
import main as tool  # noqa: E402


def _table_html(rows: list[list[str]], headers: list[str]) -> str:
    head = "".join(f"<th>{h}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows
    )
    return (
        "<html><body><table>"
        f"<tr><th>Expediente</th><th>Órgano de contratación</th>"
        f"<th>Tipo de contrato</th><th>Importe</th><th>Estado</th></tr>"
        f"{body}"
        "</table></body></html>"
    )


class ArgParsingTests(unittest.TestCase):
    def test_missing_target_returns_error(self):
        proc = subprocess.run(
            [sys.executable, str(TOOL_DIR / "main.py")],
            input=json.dumps({"request_id": "r1", "arguments": {}}),
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertFalse(payload["success"])
        self.assertEqual(payload["error"]["code"], "MISSING_TARGET")
        self.assertEqual(payload["request_id"], "r1")

    def test_invalid_json_returns_error(self):
        proc = subprocess.run(
            [sys.executable, str(TOOL_DIR / "main.py")],
            input="not json",
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertFalse(payload["success"])
        self.assertEqual(payload["error"]["code"], "INVALID_JSON")

    def test_target_type_coerced(self):
        # We can't easily stub the network here, so verify _parse_args.
        target, target_type, count, timeout = tool._parse_args({
            "target": "B12345678",
            "target_type": "CIF",  # uppercase
            "count": "10",
            "timeout_s": "42",
        })
        self.assertEqual(target, "B12345678")
        self.assertEqual(target_type, "cif")
        self.assertEqual(count, 10)
        self.assertEqual(timeout, 42)

    def test_count_and_timeout_clamped(self):
        _, _, count, timeout = tool._parse_args({
            "target": "x",
            "count": 9999,
            "timeout_s": 1,
        })
        self.assertLessEqual(count, tool.MAX_COUNT)
        self.assertGreaterEqual(timeout, tool.MIN_TIMEOUT_S)

        _, _, count, timeout = tool._parse_args({
            "target": "x",
            "count": 0,
            "timeout_s": 9999,
        })
        self.assertGreaterEqual(count, 1)
        self.assertLessEqual(timeout, tool.MAX_TIMEOUT_S)

    def test_invalid_target_type_falls_back(self):
        _, target_type, _, _ = tool._parse_args({
            "target": "x",
            "target_type": "INVALID",
        })
        self.assertEqual(target_type, "nif")


class JsPayloadTests(unittest.TestCase):
    def test_js_target_is_json_escaped(self):
        # A target that would break a naive string concatenation: contains
        # both a quote and a backslash. The result MUST keep these literal
        # at the JS layer — i.e. it must be JSON-encoded.
        nasty = 'a"b\\c</script>'
        js = tool._build_js_code(nasty, "name")
        # JSON-encode round-trip: parsing the JS-side literal reproduces
        # the original.
        self.assertIn(json.dumps(nasty), js)
        self.assertIn("submitted", js)
        self.assertIn("pickField", js)
        self.assertIn("linkFormularioBusqueda", js)

    def test_js_target_uses_target_type(self):
        js = tool._build_js_code("B12345678", "cif")
        self.assertIn('"cif"', js)


class ParseResultsTests(unittest.TestCase):
    def test_parses_well_formed_table(self):
        rows = [
            ["EXP-2026/0001", "Ministerio de Defensa", "Suministros", "1.234.567,89 EUR", "En curso"],
            ["EXP-2026/0002", "Ayuntamiento de Madrid", "Servicios", "500.000,00 EUR", "Publicado"],
        ]
        html_content = _table_html(rows, [])
        records = tool.parse_results(html_content, "EXP-2026", "name")
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["expediente"], "EXP-2026/0001")
        self.assertEqual(records[0]["organo"], "Ministerio de Defensa")
        self.assertAlmostEqual(records[0]["importe"], 1234567.89, places=2)
        self.assertEqual(records[1]["importe"], 500000.0)

    def test_nif_filter_drops_unrelated_rows(self):
        rows = [
            ["EXP-A", "Org A", "Servicios", "1.000 EUR", "—"],
            ["EXP-B", "Org B", "Servicios", "2.000 EUR", "—"],
        ]
        html_content = _table_html(rows, [])
        records = tool.parse_results(html_content, "B12345678", "nif")
        # The NIF doesn't appear in any row, so we drop both.
        self.assertEqual(records, [])

    def test_empty_or_garbage_html_returns_empty(self):
        self.assertEqual(tool.parse_results("", "x", "name"), [])
        self.assertEqual(tool.parse_results("<html>no tables</html>", "x", "name"), [])

    def test_parse_amount_handles_locale_and_spaces(self):
        self.assertTrue(abs(tool._parse_amount("1.234,56 EUR") - 1234.56) < 0.01)  # type: ignore[operator]
        self.assertTrue(abs(tool._parse_amount("1,234.56") - 1234.56) < 0.01)  # type: ignore[operator]
        self.assertTrue(abs(tool._parse_amount("  500  ") - 500.0) < 0.01)  # type: ignore[operator]
        self.assertIsNone(tool._parse_amount(""))
        self.assertIsNone(tool._parse_amount("no digits"))


class CrawlPayloadTests(unittest.TestCase):
    def test_payload_shape(self):
        payload = tool._build_payload("B12345678", "cif", 30)
        self.assertIn("url", payload)
        self.assertEqual(payload["url"], tool.BUSQUEDA_URL)
        self.assertIn(payload["result_formats"], [["html"]])
        self.assertEqual(payload["timeout"], 30)
        self.assertIn("js_code", payload)
        self.assertIn("wait_for", payload)
        # The JS payload must reference the literal target so the form fills.
        self.assertIn("B12345678", payload["js_code"])
        # The wait_for selector must be a non-empty CSS selector string.
        self.assertTrue(payload["wait_for"].strip())


class MarkdownRenderTests(unittest.TestCase):
    def test_render_includes_header_and_records(self):
        records = [
            {
                "expediente": "EXP-1",
                "organo": "Org A",
                "tipo": "Servicios",
                "procedimiento": "Abierto",
                "importe": 1234.56,
                "moneda": "EUR",
                "estado": "En curso",
                "fecha": "2026-07-01",
                "ubicacion": "Madrid",
                "url": "https://example.com/1",
                "snippet": "",
            },
        ]
        text = tool.render_markdown(records, "B12345678", "cif", {"truncated": False})
        self.assertIn("B12345678", text)
        self.assertIn("EXP-1", text)
        self.assertIn("Org A", text)
        self.assertIn("EUR", text)
        # Spanish decimal separator: 1.234,56
        self.assertIn("1.234,56", text)

    def test_render_empty_records_has_helpful_message(self):
        text = tool.render_markdown([], "x", "name", {"truncated": False})
        self.assertIn("No se encontraron", text)


class IntegrationTest(unittest.TestCase):
    """End-to-end through the subprocess wrapper, with Crawl4AI stubbed.

    The tool is invoked as ``python3 main.py`` so we cannot easily
    monkey-patch its imports from this test. We work around it by writing
    a small ``runner.py`` that imports the tool and overrides the network
    call BEFORE the CLI entrypoint runs. The stub is registered in this
    test module's globals and referenced by name from the runner.
    """

    def _run_with_stub(self, arguments: dict[str, Any], stub_body: str):
        # The runner inlines the stub's body — keeps the function's
        # closure intact without trying to pickle a nested function.
        with tempfile.TemporaryDirectory() as tmp:
            runner = Path(tmp) / "runner.py"
            runner.write_text(
                "import sys\n"
                f"sys.path.insert(0, {str(TOOL_DIR)!r})\n"
                "import main\n"
                f"{stub_body}\n"
                "main.call_crawl4ai = _STUB\n"
                "main.main()\n"
            )
            proc = subprocess.run(
                [sys.executable, str(runner)],
                input=json.dumps({"request_id": "rid", "arguments": arguments}),
                capture_output=True,
                text=True,
                timeout=10,
            )
        return proc

    @staticmethod
    def _stub_ok(html_content: str, nbytes: int) -> str:
        return (
            "def _STUB(target, target_type, timeout_s):\n"
            f"    return {html_content!r}, None, {{'crawl4ai_response_bytes': {nbytes}, 'crawl4ai_duration_ms': 12}}\n"
        )

    @staticmethod
    def _stub_fail() -> str:
        return (
            "def _STUB(target, target_type, timeout_s):\n"
            "    return None, 'Crawl4AI timeout', {'crawl4ai_url': 'http://crawl4ai:11235'}\n"
        )

    def test_full_round_trip_with_stub(self):
        rows = [["EXP-Z", "Test Org", "Suministros", "99,99 EUR", "—"]]
        html_content = _table_html(rows, [])
        proc = self._run_with_stub(
            {"target": "Test", "target_type": "name", "count": 5},
            self._stub_ok(html_content, len(html_content)),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertTrue(payload["success"])
        self.assertEqual(payload["request_id"], "rid")
        self.assertEqual(payload["structured_content"]["count"], 1)
        self.assertEqual(
            payload["structured_content"]["results"][0]["organo"], "Test Org"
        )
        # The markdown content block should mention the org and the EUR import.
        content = payload["content"][0]["text"]
        self.assertIn("Test Org", content)
        self.assertIn("99,99", content)

    def test_full_round_trip_crawl4ai_error(self):
        proc = self._run_with_stub({"target": "X"}, self._stub_fail())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertFalse(payload["success"])
        self.assertEqual(payload["error"]["code"], "SEARCH_FAILED")
        self.assertIn("Crawl4AI timeout", payload["error"]["message"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
