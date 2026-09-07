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

logger = get_logger(__name__, "boe_search")

BOE_API = "https://www.boe.es/datosabiertos/api/boe/sumario"


def _as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def iter_boe_items(data: dict) -> list[dict]:
    """Flatten the nested BOE sumario into enriched item dicts.

    Real path: data.sumario.diario[].seccion[].departamento[].epigrafe[].item[].
    Each returned item carries extra keys 'seccion' (section code) and
    'epigrafe' (epigraph name) for evidence metadata.
    """
    out: list[dict] = []
    try:
        diario = data["data"]["sumario"]["diario"]
    except (KeyError, TypeError):
        return out
    for day in _as_list(diario):
        for seccion in _as_list(day.get("seccion")):
            sec_code = seccion.get("codigo", "")
            for dep in _as_list(seccion.get("departamento")):
                for ep in _as_list(dep.get("epigrafe")):
                    for it in _as_list(ep.get("item")):
                        if not isinstance(it, dict):
                            continue
                        enriched = dict(it)
                        enriched["seccion"] = sec_code
                        enriched["epigrafe"] = ep.get("nombre", "")
                        out.append(enriched)
    return out


def canonical_item_url(item: dict) -> str:
    """Return url_html, falling back to url_pdf.texto."""
    url = str(item.get("url_html", "") or "")
    if not url:
        pdf = item.get("url_pdf")
        if isinstance(pdf, dict):
            url = str(pdf.get("texto", "") or "")
    return url


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
        resp = request_with_retry(
            "GET", url, headers={"Accept": "application/json"}, timeout=15
        )
    except Exception as exc:
        logger.warning(
            "BOE request failed",
            extra_data={"date": date_str, "error": str(exc)},
        )
        return []
    if resp.status_code != 200:
        return []
    try:
        return iter_boe_items(resp.json())
    except Exception as exc:
        logger.warning(
            "BOE parse failed",
            extra_data={"date": date_str, "error": str(exc)},
        )
        return []


def matches_target(item: dict, target: str, target_type: str) -> bool:
    titulo = str(item.get("titulo", ""))
    if not titulo:
        return False
    if target_type in ("nif", "cif"):
        return target.upper() in titulo.upper()
    return target.lower() in titulo.lower()


def search_boe(target: str, target_type: str, date_from: str, date_to: str):
    if not REQUESTS_AVAILABLE:
        return None, "requests library not available"
    dates = date_range(date_from, date_to)
    if len(dates) > 90:
        return None, "date range too large (max 90 days)"
    results = []
    for d in dates:
        day_iso = datetime.strptime(d, "%Y%m%d").strftime("%Y-%m-%d")
        for item in fetch_boe_day(d):
            if matches_target(item, target, target_type):
                results.append(build_evidence(
                    source="boe",
                    official=True,
                    confidence=0.9,
                    title=str(item.get("titulo", "")),
                    date=day_iso,
                    url=canonical_item_url(item),
                    metadata={
                        "identificador": item.get("identificador", ""),
                        "seccion": item.get("seccion", ""),
                        "epigrafe": item.get("epigrafe", ""),
                    },
                    entity=target,
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
        results, error = search_boe(target, target_type, date_from, date_to)
        if error:
            write_response({"success": False, "request_id": request_id,
                            "error": {"code": "SEARCH_FAILED", "message": error}})
            return
        lines = [f"**BOE — Disposiciones y anuncios para {target}**\n"]
        for r in results:
            lines.append(f"Fecha: {r['date']}")
            lines.append(f"**{r['title']}**")
            if r.get("description"):
                lines.append(r["description"])
            lines.append(f"URL: {r['url']}\n")
        if not results:
            lines.append("No se encontraron resultados.")
        write_response({
            "success": True, "request_id": request_id,
            "content": [{"type": "text", "text": "\n".join(lines)}],
            "structured_content": build_search_result(
                source="boe", target=target, results=results),
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
