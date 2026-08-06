#!/usr/bin/env python3
import json
import os
import sys
from datetime import datetime, timedelta
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common.structured_logging import get_logger
from common.http import request_with_retry, REQUESTS_AVAILABLE

logger = get_logger(__name__, "boe_search")

BOE_API = "https://www.boe.es/datosabiertos/api/boe/sumario"


def date_range(from_str: str, to_str: str) -> list[str]:
    fmt = "%Y-%m-%d"
    start = datetime.strptime(from_str, fmt) if from_str else datetime.now() - timedelta(days=30)
    end = datetime.strptime(to_str, fmt) if to_str else datetime.now()
    dates = []
    current = start
    while current <= end:
        dates.append(current.strftime("%Y%m%d"))
        current += timedelta(days=1)
    return dates


def fetch_boe_day(date_str: str) -> list[dict]:
    url = f"{BOE_API}/{date_str}"
    try:
        resp = request_with_retry("GET", url, headers={"Accept": "application/json"}, timeout=15)
        if resp.status_code != 200:
            return []
        data = resp.json()
        items = data.get("item", []) or []
        if isinstance(items, dict):
            items = [items]
        return items
    except Exception:
        return []


def matches_target(item: dict, target: str, target_type: str) -> bool:
    target_upper = target.upper()
    search_fields = [
        str(item.get("titulo", "")),
        str(item.get("contenido", "")),
        str(item.get("url", "")),
    ]
    combined = " ".join(search_fields).upper()
    if target_type in ("nif", "cif"):
        return target_upper in combined
    else:
        return target.lower() in combined.lower()


def search_boe(target: str, target_type: str, date_from: str, date_to: str):
    if not REQUESTS_AVAILABLE:
        return None, "requests library not available"
    dates = date_range(date_from, date_to)
    if len(dates) > 90:
        return None, "date range too large (max 90 days)"
    results = []
    for d in dates:
        items = fetch_boe_day(d)
        for item in items:
            if matches_target(item, target, target_type):
                results.append({
                    "date": d,
                    "titulo": item.get("titulo", ""),
                    "url": item.get("url", ""),
                    "contenido": item.get("contenido", "")[:300],
                })
    return results, None


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
        results, error = search_boe(target, target_type, date_from, date_to)
        if error:
            write_response({"success": False, "request_id": request_id,
                            "error": {"code": "SEARCH_FAILED", "message": error}})
            return
        lines = [f"**BOE — Disposiciones y anuncios para {target}**\n"]
        for r in results:
            lines.append(f"Fecha: {r['date']}")
            lines.append(f"**{r['titulo']}**")
            if r["contenido"]:
                lines.append(r["contenido"])
            lines.append(f"URL: {r['url']}\n")
        if not results:
            lines.append("No se encontraron resultados.")
        write_response({
            "success": True, "request_id": request_id,
            "content": [{"type": "text", "text": "\n".join(lines)}],
            "structured_content": {"source": "boe", "target": target, "results": results, "count": len(results)},
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
