#!/usr/bin/env python3
import json
import os
import sys
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common.structured_logging import get_logger

logger = get_logger(__name__, "bdns_search")

BDNS_URL = "https://www.pap.hacienda.gob.es/bdnstrans"
BDNS_SEARCH_URL = f"{BDNS_URL}/api/consulta-beneficiarios"


def search_bdns(target: str, target_type: str):
    return None, "BDNS scraping not yet implemented — requires JSF ViewState handling"


def write_response(data: dict[str, Any]) -> None:
    print(json.dumps(data, default=str), flush=True)


def main() -> None:
    request: dict = {}
    try:
        request = json.loads(sys.stdin.read())
        request_id = request.get("request_id", "")
        args = request.get("arguments", {})
        target = str(args.get("target", "")).strip()
        if not target:
            write_response({"success": False, "request_id": request_id,
                            "error": {"code": "MISSING_TARGET", "message": "target is required"}})
            return
        target_type = args.get("target_type", "nif")
        results, error = search_bdns(target, target_type)
        if error:
            write_response({"success": False, "request_id": request_id,
                            "error": {"code": "SEARCH_FAILED", "message": error}})
            return
        write_response({
            "success": False, "request_id": request_id,
            "error": {"code": "NOT_IMPLEMENTED", "message": "BDNS scraping not yet implemented"},
        })
    except json.JSONDecodeError:
        write_response({"success": False, "request_id": "",
                        "error": {"code": "INVALID_JSON", "message": "Failed to parse JSON"}})
    except Exception as e:
        logger.error("Unhandled exception", extra_data={"error": str(e)})
        write_response({"success": False, "request_id": request.get("request_id", ""),
                        "error": {"code": "EXECUTION_FAILED", "message": str(e)}})


if __name__ == "__main__":
    main()
