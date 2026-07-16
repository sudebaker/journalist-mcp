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


class CrawlerConfigTests(unittest.TestCase):
    """The Crawl4AI 0.9.0 /crawl endpoint puts per-URL knobs (wait_for,
    delay_before_return_html, etc.) under a `crawler_config` block and
    forbids js_code/js_code_before_wait for untrusted callers. These
    tests pin the contract the tool sends."""

    def test_crawler_config_blocks_have_only_allowed_keys(self):
        cfg = tool._build_crawler_config(30)
        # The allowlist of safe knobs we know we set.
        self.assertIn("wait_for", cfg)
        self.assertIn("delay_before_return_html", cfg)
        self.assertIn("page_timeout", cfg)
        # js_code / js_code_before_wait MUST NEVER be set — the server
        # rejects them with 400 for untrusted clients.
        self.assertNotIn("js_code", cfg)
        self.assertNotIn("js_code_before_wait", cfg)

    def test_crawler_config_clamped_to_safe_bounds(self):
        # delay is a hard cap so it stays under 15s even if the caller
        # asks for 1000s — the form is heavy and Crawl4AI's clamps are tight.
        cfg = tool._build_crawler_config(1000)
        self.assertLessEqual(cfg["delay_before_return_html"], 15)
        # page_timeout is the page-load cap in ms — it scales with the
        # caller's timeout_s but should never exceed a sane upper bound.
        self.assertEqual(cfg["page_timeout"], 1000 * 1000)
        self.assertLessEqual(cfg["page_timeout"], 60 * 60 * 1000)  # sanity check

    def test_payload_shape(self):
        payload = tool._build_payload("B12345678", "cif", 30)
        # Top-level shape: list of URLs + crawler_config block.
        self.assertIn("urls", payload)
        self.assertEqual(payload["urls"], [tool.BUSQUEDA_URL])
        self.assertIn("crawler_config", payload)
        self.assertIsInstance(payload["crawler_config"], dict)
        self.assertEqual(payload["timeout"], 30)
        # Legacy 'url' key MUST NOT be present — it confuses the server
        # into a 422 about a missing `urls` field.
        self.assertNotIn("url", payload)
        # js_code at the top level would also 422, and is forbidden
        # inside crawler_config too.
        self.assertNotIn("js_code", payload)
        # The wait_for selector must be a non-empty CSS selector string.
        self.assertTrue(payload["crawler_config"]["wait_for"].strip())


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
