#!/usr/bin/env python3
import json
import os
import sys
from datetime import datetime, timedelta
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common.structured_logging import get_logger

logger = get_logger(__name__, "ted_search")

try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False

TED_API_URL = "https://api.ted.europa.eu/v3/notices/search"


def build_expert_query(target: str, target_type: str, date_from: str, date_to: str) -> str:
    if target_type in ("nif", "cif"):
        parts = [f'organisation-identifier-buyer="{target}"']
    else:
        parts = [f'organisation-name-buyer="{target}"']
    if date_from and date_to:
        parts.append(f'PD>={date_from} AND PD<={date_to}')
    return " AND ".join(parts)


def search_ted(target: str, target_type: str, date_from: str, date_to: str, count: int):
    if not REQUESTS_AVAILABLE:
        return None, "requests library not available"
    if not date_from:
        date_from = (datetime.now() - timedelta(days=365)).strftime("%Y%m%d")
    else:
        date_from = date_from.replace("-", "")
    if not date_to:
        date_to = datetime.now().strftime("%Y%m%d")
    else:
        date_to = date_to.replace("-", "")
    query = build_expert_query(target, target_type, date_from, date_to)
    payload = {
        "query": query,
        "fields": ["ND", "publication-date", "organisation-name-buyer", "organisation-identifier-buyer"],
        "limit": min(count, 100),
    }
    try:
        resp = requests.post(TED_API_URL, json=payload, timeout=30, headers={"Accept": "application/json"})
        if resp.status_code != 200:
            return None, f"TED API returned HTTP {resp.status_code}"
        data = resp.json()
        notices = data.get("notices", [])
        results = []
        for n in notices[:count]:
            results.append({
                "notice_id": n.get("ND", ""),
                "title": n.get("notice-title", ""),
                "publication_date": n.get("publication-date", ""),
                "organization": n.get("organisation-name-buyer", ""),
                "org_national_id": n.get("organisation-identifier-buyer", ""),
            })
        return results, None
    except requests.exceptions.Timeout:
        return None, "TED API timed out"
    except Exception as e:
        return None, f"TED search failed: {e}"


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
        date_from = args.get("date_from", "")
        date_to = args.get("date_to", "")
        count = int(args.get("count", 20))
        results, error = search_ted(target, target_type, date_from, date_to, count)
        if error:
            write_response({"success": False, "request_id": request_id,
                            "error": {"code": "SEARCH_FAILED", "message": error}})
            return
        lines = [f"**TED — Licitaciones UE para {target}**\n"]
        for i, r in enumerate(results, 1):
            lines.append(f"**{i}. {r['title']}**")
            lines.append(f"Fecha: {r['publication_date']} | Org: {r['organization']} ({r['org_national_id']})")
            lines.append(f"ID: {r['notice_id']}\n")
        if not results:
            lines.append("No se encontraron resultados.")
        write_response({
            "success": True, "request_id": request_id,
            "content": [{"type": "text", "text": "\n".join(lines)}],
            "structured_content": {"source": "ted", "target": target, "results": results, "count": len(results)},
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
