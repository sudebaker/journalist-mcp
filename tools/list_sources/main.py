#!/usr/bin/env python3
"""
List Sources Tool for MCP Orchestrator.
Lists available data sources and their status.
"""

import json
import os
import sys
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from common.structured_logging import get_logger

logger = get_logger(__name__, "list_sources")

SOURCES = [
    {"name": "boe", "description": "BOE - Boletín Oficial del Estado", "status": "available"},
    {"name": "borme", "description": "BORME - Boletín Oficial del Registro Mercantil", "status": "available"},
    {"name": "bdns", "description": "BDNS - Base de Datos Nacional de Subvenciones", "status": "available"},
    {"name": "ckan", "description": "CKAN - Catálogos de datos abiertos", "status": "available"},
    {"name": "cohesion", "description": "Cohesion Data - Fondos de cohesión europeos", "status": "available"},
    {"name": "contratacion", "description": "Contratación del Sector Público", "status": "available"},
    {"name": "datosgob", "description": "datos.gob.es - Portal de datos abiertos del Gobierno", "status": "available"},
    {"name": "fts", "description": "FTS - Financial Tracking Service", "status": "available"},
    {"name": "pscp", "description": "PSCP - Plataforma de Contratación del Sector Público", "status": "available"},
    {"name": "ted", "description": "TED - Tenders Electronic Daily", "status": "available"},
    {"name": "transparency", "description": "Transparency Portal - Portal de transparencia AGE", "status": "available"},
    {"name": "searxng_search", "description": "SearXNG - Búsqueda web privada", "status": "available"},
    {"name": "browserless_render", "description": "Browserless - Renderizado de páginas web con Chrome headless", "status": "available"},
]


def read_request() -> dict[str, Any]:
    return json.loads(sys.stdin.read())


def write_response(data: dict[str, Any]) -> None:
    print(json.dumps(data, default=str), flush=True)


def main() -> None:
    request: dict = {}
    try:
        request = read_request()
        request_id = request.get("request_id", "")

        write_response({
            "success": True,
            "request_id": request_id,
            "content": [{"type": "text", "text": json.dumps(SOURCES, indent=2)}],
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
