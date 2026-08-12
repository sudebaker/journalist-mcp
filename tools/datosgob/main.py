#!/usr/bin/env python3
"""datosgob_fetch — fetch open-data datasets from datos.gob.es (Spanish Open Data Portal).

The portal exposes a Linked Data API at https://datos.gob.es/apidata whose
``/catalog/dataset`` endpoint supports pagination via ``_pageSize`` and ``_page``.
The server does NOT accept a free-text ``q`` parameter (it returns HTTP 400 for
unknown shortnames) and the reserved ``_search`` parameter returns an empty
items list. So we page through the catalog and apply a client-side
case-insensitive filter on title / keyword / description / publisher. When
``query`` is empty we simply return the first page.
"""
import json
import os
import sys
from typing import Any
from urllib.parse import urlencode

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common.structured_logging import get_logger

logger = get_logger(__name__, "datosgob_fetch")

from common.http import request_with_retry, REQUESTS_AVAILABLE

DATOSGOB_API_BASE = "https://datos.gob.es/apidata/catalog/dataset"
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100
MAX_SCAN_PAGES = 20  # hard cap so a non-matching query can't burn the budget
HTTP_TIMEOUT = 30
USER_AGENT = "journalist-mcp/datosgob_fetch (1.0.0)"


def _localized_text(field: Any) -> str:
    """Normalize the { _value, _lang } objects / lists / strings used by the API."""
    if field is None:
        return ""
    if isinstance(field, str):
        return field
    if isinstance(field, list):
        return " ".join(_localized_text(v) for v in field)
    if isinstance(field, dict):
        return str(field.get("_value", ""))
    return str(field)


def _dataset_to_result(item: dict) -> dict:
    """Project a datos.gob.es dataset item down to the journalist-mcp shape."""
    distribution = item.get("distribution") or {}
    if isinstance(distribution, list):
        distribution = distribution[0] if distribution else {}
    fmt = distribution.get("format", "")
    if isinstance(fmt, dict):
        fmt = fmt.get("value", "") or fmt.get("_about", "")
    access_url = distribution.get("accessURL", "")
    keywords_raw = item.get("keyword") or []
    keywords = [_localized_text(k) for k in keywords_raw if k]
    themes_raw = item.get("theme") or []
    themes = [t.get("_about", t) if isinstance(t, dict) else str(t) for t in themes_raw]
    return {
        "id": item.get("identifier") or item.get("_about", ""),
        "url": item.get("_about", ""),
        "title": _localized_text(item.get("title")),
        "description": _localized_text(item.get("description")),
        "publisher": item.get("publisher", ""),
        "license": item.get("license", ""),
        "modified": item.get("modified", ""),
        "issued": item.get("issued", ""),
        "language": item.get("language", ""),
        "format": fmt,
        "access_url": access_url,
        "keywords": keywords,
        "themes": themes,
    }


def _matches(item: dict, needle: str) -> bool:
    """Case-insensitive substring match across the user-facing fields."""
    haystack_parts = [
        _localized_text(item.get("title")),
        _localized_text(item.get("description")),
        _localized_text(item.get("keyword")),
        str(item.get("publisher") or ""),
    ]
    haystack = " ".join(haystack_parts).lower()
    return needle.lower() in haystack


def search_datosgob(query: str, rows: int) -> tuple[list[dict] | None, str | None]:
    if not REQUESTS_AVAILABLE:
        return None, "requests library not available"
    rows = max(1, min(rows, MAX_PAGE_SIZE))
    needle = query.strip()
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    matched: list[dict] = []
    scanned = 0
    try:
        if not needle:
            params = {"_pageSize": str(rows), "_page": "0"}
            url = f"{DATOSGOB_API_BASE}?{urlencode(params)}"
            resp = request_with_retry("GET", url, headers=headers, timeout=HTTP_TIMEOUT)
            if resp.status_code != 200:
                return None, f"datos.gob.es API returned HTTP {resp.status_code}"
            payload = resp.json()
            items = payload.get("result", {}).get("items", []) or []
            return [_dataset_to_result(i) for i in items[:rows]], None

        for page in range(MAX_SCAN_PAGES):
            params = {"_pageSize": str(DEFAULT_PAGE_SIZE), "_page": str(page)}
            url = f"{DATOSGOB_API_BASE}?{urlencode(params)}"
            resp = request_with_retry("GET", url, headers=headers, timeout=HTTP_TIMEOUT)
            if resp.status_code != 200:
                return None, f"datos.gob.es API returned HTTP {resp.status_code} on page {page}"
            payload = resp.json()
            items = payload.get("result", {}).get("items", []) or []
            scanned += len(items)
            for it in items:
                if _matches(it, needle):
                    matched.append(_dataset_to_result(it))
                    if len(matched) >= rows:
                        return matched, None
            if not items:
                break  # catalog exhausted
        return matched, None
    except Exception as e:
        msg = str(e).lower()
        if "timeout" in msg or "timed" in msg:
            return None, "datos.gob.es API timed out"
        return None, f"datos.gob.es fetch failed: {e}"
    finally:
        if needle:
            logger.info(
                "datosgob scan complete",
                extra_data={"query": needle, "scanned_items": scanned, "matched": len(matched)},
            )


def write_response(data: dict[str, Any]) -> None:
    print(json.dumps(data, default=str), flush=True)


def main() -> None:
    request: dict = {}
    try:
        request = json.loads(sys.stdin.read() or "{}")
        request_id = request.get("request_id", "")
        args = request.get("arguments", {}) or {}
        query = str(args.get("query", "")).strip()
        rows = args.get("rows", DEFAULT_PAGE_SIZE)
        try:
            rows = int(rows)
        except (TypeError, ValueError):
            write_response({
                "success": False, "request_id": request_id,
                "error": {"code": "INVALID_ROWS", "message": "rows must be an integer"},
            })
            return

        results, error = search_datosgob(query, rows)
        if error:
            write_response({
                "success": False, "request_id": request_id,
                "error": {"code": "FETCH_FAILED", "message": error},
            })
            return

        results = results or []
        lines = [f"**datos.gob.es — Datasets para {query or '*'}**\n"]
        for i, r in enumerate(results, 1):
            title = r["title"] or "(sin título)"
            publisher = r["publisher"].rsplit("/", 1)[-1] or r["publisher"]
            lines.append(f"**{i}. {title}**")
            lines.append(f"Editor: {publisher} | Modificado: {r['modified'] or r['issued']}")
            if r["keywords"]:
                lines.append(f"Keywords: {', '.join(r['keywords'][:5])}")
            lines.append(f"URL: {r['url']}\n")
        if not results:
            lines.append("No se encontraron resultados.")

        write_response({
            "success": True, "request_id": request_id,
            "content": [{"type": "text", "text": "\n".join(lines)}],
            "structured_content": {
                "source": "datos.gob.es",
                "query": query,
                "results": results,
                "count": len(results),
            },
        })
    except json.JSONDecodeError:
        write_response({
            "success": False, "request_id": "",
            "error": {"code": "INVALID_JSON", "message": "Failed to parse JSON"},
        })
    except Exception as e:
        logger.error("Unhandled exception", extra_data={"error": str(e)})
        write_response({
            "success": False,
            "request_id": request.get("request_id", "") if isinstance(request, dict) else "",
            "error": {"code": "EXECUTION_FAILED", "message": str(e)},
        })


if __name__ == "__main__":
    main()
