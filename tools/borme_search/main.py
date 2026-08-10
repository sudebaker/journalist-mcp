#!/usr/bin/env python3
import json
import os
import sys
from datetime import datetime, timedelta
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common.structured_logging import get_logger
from common.http import request_with_retry, REQUESTS_AVAILABLE
from common.evidence import build_evidence, build_search_result

logger = get_logger(__name__, "borme_search")

BORME_API = "https://www.boe.es/datosabiertos/api/borme/sumario"


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


def fetch_borme_day(date_str: str) -> list[dict]:
    url = f"{BORME_API}/{date_str}"
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
    if target_type in ("nif", "cif"):
        nif = str(item.get("nif", "")).upper()
        return target.upper() in nif or nif == target.upper()
    else:
        nombre = str(item.get("nombre", "")).lower()
        return target.lower() in nombre


def search_borme(target: str, target_type: str, date_from: str, date_to: str):
    if not REQUESTS_AVAILABLE:
        return None, "requests library not available"
    dates = date_range(date_from, date_to)
    if len(dates) > 90:
        return None, "date range too large (max 90 days)"
    results = []
    for d in dates:
        items = fetch_borme_day(d)
        for item in items:
            if matches_target(item, target, target_type):
                url = item.get("url", "")
                nombre = item.get("nombre", "")
                results.append(build_evidence(
                    source="borme",
                    official=True,
                    confidence=0.9,
                    title=nombre,
                    date=d,
                    url=url,
                    entity=target,
                    raw={
                        "nif": item.get("nif", ""),
                        "name": nombre,
                        "actos": item.get("actos", []),
                    },
                    query=target,
                ))
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
        results, error = search_borme(target, target_type, date_from, date_to)
        if error:
            write_response({"success": False, "request_id": request_id,
                            "error": {"code": "SEARCH_FAILED", "message": error}})
            return
        lines = [f"**BORME — Actos mercantiles para {target}**\n"]
        for r in results:
            raw = r.get("raw", {})
            lines.append(f"Fecha: {r['date']} | {r['title']} ({raw.get('nif', '')})")
            for acto in raw.get("actos", []):
                lines.append(f"  → {acto}")
            lines.append("")
        if not results:
            lines.append("No se encontraron resultados.")
        write_response({
            "success": True, "request_id": request_id,
            "content": [{"type": "text", "text": "\n".join(lines)}],
            "structured_content": build_search_result(
                source="borme", target=target, results=results),
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
