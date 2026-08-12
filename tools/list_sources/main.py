#!/usr/bin/env python3
"""List Sources Tool for MCP Orchestrator.

Discovers available data sources dynamically by scanning sibling tool manifests
(tools/*/tool.yaml). A tool is considered a data source when its name ends with
_search, _fetch or _render.
"""

import glob
import json
import os
import sys
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from common.structured_logging import get_logger

logger = get_logger(__name__, "list_sources")

_SOURCE_SUFFIXES = ("_search", "_fetch", "_render")


def _is_source_tool(name: str) -> bool:
    """Return True if the tool name identifies a data-source tool."""
    return name.endswith(_SOURCE_SUFFIXES)


def _discover_sources() -> list[dict[str, str]]:
    """Scan tool manifests and return data sources sorted by name."""
    tools_dir = os.path.dirname(os.path.dirname(__file__))
    manifest_pattern = os.path.join(tools_dir, "*", "tool.yaml")

    sources: list[dict[str, str]] = []
    for manifest_path in glob.glob(manifest_pattern):
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                import yaml

                manifest = yaml.safe_load(f)
        except Exception as exc:
            logger.warning(
                "failed_to_read_manifest",
                extra_data={"path": manifest_path, "error": str(exc)},
            )
            continue

        if not isinstance(manifest, dict):
            continue

        name = manifest.get("name", "")
        description = manifest.get("description", "")
        if not isinstance(name, str) or not isinstance(description, str):
            continue
        if not _is_source_tool(name):
            continue

        sources.append({
            "name": name,
            "description": description,
            "status": "available",
        })

    sources.sort(key=lambda s: s["name"])
    return sources


def read_request() -> dict[str, Any]:
    return json.loads(sys.stdin.read())


def write_response(data: dict[str, Any]) -> None:
    print(json.dumps(data, default=str), flush=True)


def main() -> None:
    request: dict = {}
    try:
        request = read_request()
        request_id = request.get("request_id", "")

        sources = _discover_sources()

        write_response({
            "success": True,
            "request_id": request_id,
            "content": [{"type": "text", "text": json.dumps(sources, indent=2)}],
            "metadata": {"tool": "list_sources"},
        })

    except json.JSONDecodeError:
        write_response({
            "success": False,
            "request_id": "",
            "error": {"code": "INVALID_JSON", "message": "Failed to parse JSON request"},
        })
    except Exception as e:
        logger.error("Unhandled exception", extra_data={"error": str(e)})
        write_response({
            "success": False,
            "request_id": "",
            "error": {"code": "EXECUTION_FAILED", "message": str(e)},
        })


if __name__ == "__main__":
    main()
