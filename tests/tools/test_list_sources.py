#!/usr/bin/env python3
"""Tests for the list_sources tool.

list_sources must discover data-source tools dynamically from tool manifests
instead of returning a hardcoded list.
"""

import json
import os
import subprocess
import sys
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))

from list_sources.main import _discover_sources, _is_source_tool


_SOURCE_SUFFIXES = ("_search", "_fetch", "_render")


def test_is_source_tool_accepts_search_fetch_render() -> None:
    assert _is_source_tool("boe_search") is True
    assert _is_source_tool("boe_fetch") is True
    assert _is_source_tool("crawl4ai_render") is True


def test_is_source_tool_rejects_infrastructure_tools() -> None:
    assert _is_source_tool("echo") is False
    assert _is_source_tool("datetime") is False
    assert _is_source_tool("rustfs_storage") is False
    assert _is_source_tool("list_sources") is False


def test_is_source_tool_rejects_partial_matches() -> None:
    assert _is_source_tool("search_engine") is False
    assert _is_source_tool("my_fetcher") is False


def test_discover_sources_returns_expected_tools() -> None:
    sources = _discover_sources()
    names = {s["name"] for s in sources}

    # New *_search tools added after the original hardcoded list.
    assert "doue_search" in names
    assert "transparency_search" in names
    assert "bdns_search" in names
    assert "contratacion_search" in names

    # *_fetch tools.
    assert "boe_fetch" in names
    assert "borme_fetch" in names
    assert "datosgob_fetch" in names
    assert "fts_fetch" in names

    # *_render tools.
    assert "crawl4ai_render" in names

    # Infrastructure / utility tools must not appear.
    assert "echo" not in names
    assert "datetime" not in names
    assert "rustfs_storage" not in names
    assert "list_sources" not in names
    assert "case_evidence" not in names
    assert "cross_reference" not in names

    # Expected cardinality: 8 _search + 11 _fetch + 1 _render.
    assert len(sources) == 20


def test_discover_sources_includes_description() -> None:
    sources = _discover_sources()
    assert len(sources) > 0
    for source in sources:
        assert "name" in source
        assert "description" in source
        assert isinstance(source["name"], str)
        assert isinstance(source["description"], str)
        assert source["name"]


def test_discover_sources_is_sorted_by_name() -> None:
    sources = _discover_sources()
    names = [s["name"] for s in sources]
    assert names == sorted(names)


def _run_subprocess(payload: dict[str, Any]) -> dict[str, Any]:
    repo_root = os.path.join(os.path.dirname(__file__), "..", "..")
    proc = subprocess.run(
        ["python3", "tools/list_sources/main.py"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=repo_root,
    )
    assert proc.returncode == 0, f"subprocess failed: {proc.stderr}"
    return json.loads(proc.stdout)


def test_subprocess_returns_success_with_discovered_sources() -> None:
    result = _run_subprocess({"request_id": "t"})
    assert result["success"] is True
    assert result["request_id"] == "t"

    # The tool returns the source list as a JSON string inside the text content.
    content = result["content"]
    assert len(content) == 1
    assert content[0]["type"] == "text"
    sources = json.loads(content[0]["text"])
    names = {s["name"] for s in sources}
    assert "doue_search" in names
    assert "transparency_search" in names


def test_subprocess_rejects_invalid_json() -> None:
    repo_root = os.path.join(os.path.dirname(__file__), "..", "..")
    proc = subprocess.run(
        ["python3", "tools/list_sources/main.py"],
        input="not valid json",
        capture_output=True,
        text=True,
        cwd=repo_root,
    )
    assert proc.returncode == 0
    result = json.loads(proc.stdout)
    assert result["success"] is False
    assert result["error"]["code"] == "INVALID_JSON"
