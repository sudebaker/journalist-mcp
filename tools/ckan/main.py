#!/usr/bin/env python3
"""ckan_fetch — multi-portal CKAN dataset search.

Queries the CKAN Action API on several Spanish open-data portals and returns
a unified result set. Per-portal failures are surfaced as warnings but do not
fail the whole request — as long as at least one portal returns something,
the overall response is `success=True`.

Output: SubprocessResponse JSON to STDOUT (matches other journalist-mcp tools).
"""
import json
import os
import sys
from typing import Any
from urllib.parse import urlencode

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common.structured_logging import get_logger

logger = get_logger(__name__, "ckan_fetch")

try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False


# Default interest tags for the journalist workflow.
DEFAULT_TAGS = ("contratacion", "subvenciones", "presupuestos")

# Known CKAN portals. `api_base` is the root for the Action API
# (POST/GET .../api/3/action/<action>). `id` is the short label we report back.
PORTALS: dict[str, dict[str, str]] = {
    "madrid": {
        "name": "Ayuntamiento de Madrid",
        "api_base": "https://datos.madrid.es/api/3/action",
    },
    "barcelona": {
        "name": "Ajuntament de Barcelona",
        "api_base": "https://opendata-ajuntament.barcelona.cat/data/api/3/action",
    },
    "euskadi": {
        "name": "Gobierno Vasco - Open Data Euskadi",
        "api_base": "https://opendata.euskadi.eus/api/3/action",
    },
}

MAX_ROWS = 50
DEFAULT_ROWS = 10
DEFAULT_TIMEOUT = 20  # seconds per portal


def _build_fq(tags: list[str]) -> str:
    """Build CKAN `fq` filter for tags (OR semantics across tags).

    Uses Solr's `tags:` field; multiple values are OR-combined.
    """
    if not tags:
        return ""
    # Quote and escape each tag value, then OR-join.
    quoted = []
    for t in tags:
        # Strip any quotes the user may have included.
        clean = t.replace('"', "").strip()
        if not clean:
            continue
        quoted.append(f'tags:"{clean}"')
    if not quoted:
        return ""
    return " OR ".join(quoted)


def _search_portal(
    portal_id: str,
    portal_cfg: dict[str, str],
    query: str,
    tags: list[str],
    rows: int,
    timeout: int,
) -> tuple[list[dict[str, Any]], str | None]:
    """Run `package_search` against one portal. Returns (results, error)."""
    if not REQUESTS_AVAILABLE:
        return [], "requests library not available"

    fq = _build_fq(tags)
    params: dict[str, str] = {
        "q": query,
        "rows": str(max(1, min(rows, MAX_ROWS))),
    }
    if fq:
        params["fq"] = fq
    url = f"{portal_cfg['api_base']}/package_search?{urlencode(params)}"

    try:
        resp = requests.get(url, timeout=timeout, headers={"Accept": "application/json"})
    except requests.exceptions.Timeout:
        return [], f"{portal_id}: timed out after {timeout}s"
    except requests.exceptions.ConnectionError as e:
        return [], f"{portal_id}: connection error ({type(e).__name__})"
    except Exception as e:
        return [], f"{portal_id}: request failed ({type(e).__name__}: {e})"

    if resp.status_code != 200:
        return [], f"{portal_id}: HTTP {resp.status_code}"

    try:
        data = resp.json()
    except ValueError:
        return [], f"{portal_id}: non-JSON response"

    if not data.get("success"):
        return [], f"{portal_id}: CKAN returned success=false ({data.get('error', {})})"

    raw = (data.get("result") or {}).get("results") or []
    results: list[dict[str, Any]] = []
    for item in raw:
        # Normalize: keep only the fields we know are useful, regardless of
        # which portal produced them.
        results.append({
            "portal": portal_id,
            "portal_name": portal_cfg["name"],
            "id": item.get("id") or item.get("name") or "",
            "name": item.get("name") or "",
            "title": item.get("title") or "",
            "notes": item.get("notes") or "",
            "organization": (item.get("organization") or {}).get("title")
                or (item.get("author") or ""),
            "tags": [t.get("display_name") or t.get("name", "")
                     for t in (item.get("tags") or []) if t],
            "license": ((item.get("license_title") or "")
                        or (item.get("license_id") or "")),
            "num_resources": len(item.get("resources") or []),
            "metadata_modified": item.get("metadata_modified", ""),
            "url": item.get("url") or "",
        })
    return results, None


def search_all(
    portal_filter: list[str] | None,
    query: str,
    tags: list[str],
    rows: int,
    timeout: int,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Query all matching portals. Returns (results, errors_by_portal)."""
    if portal_filter:
        targets = {p: PORTALS[p] for p in portal_filter if p in PORTALS}
        unknown = [p for p in portal_filter if p not in PORTALS]
        if unknown:
            logger.warning(
                "Unknown portal(s) requested; ignored",
                extra_data={"unknown": unknown, "known": list(PORTALS.keys())},
            )
    else:
        targets = PORTALS

    if not targets:
        return [], {"_config": "no valid portals to query"}

    all_results: list[dict[str, Any]] = []
    errors: dict[str, str] = {}

    if not targets:
        return [], {"_config": "no valid portals to query"}

    for pid, pcfg in targets.items():
        results, err = _search_portal(pid, pcfg, query, tags, rows, timeout)
        if err:
            errors[pid] = err
            logger.warning(
                "CKAN portal search failed",
                extra_data={"portal": pid, "error": err},
            )
        all_results.extend(results)

    return all_results, errors


def _format_markdown(query: str, results: list[dict[str, Any]], errors: dict[str, str]) -> str:
    lines: list[str] = [f"**CKAN — Datasets para `{query}`**\n"]
    if not results:
        lines.append("No se encontraron resultados en los portales consultados.")
    else:
        # Group by portal for readability.
        by_portal: dict[str, list[dict[str, Any]]] = {}
        for r in results:
            by_portal.setdefault(r["portal"], []).append(r)
        for pid, items in by_portal.items():
            portal_name = items[0]["portal_name"]
            lines.append(f"### {portal_name} ({len(items)})")
            for i, r in enumerate(items, 1):
                title = r["title"] or r["name"] or r["id"]
                org = r["organization"] or "(sin organismo)"
                tag_str = ", ".join(t for t in r["tags"] if t)
                tag_str = f" — tags: {tag_str}" if tag_str else ""
                lines.append(f"**{i}. {title}**")
                lines.append(f"Organismo: {org} | Recursos: {r['num_resources']}{tag_str}")
                if r["url"]:
                    lines.append(f"URL: {r['url']}")
                if r["notes"]:
                    snippet = r["notes"]
                    if len(snippet) > 240:
                        snippet = snippet[:237] + "..."
                    lines.append(f"{snippet}")
                lines.append("")
    if errors:
        lines.append("---")
        lines.append("**Avisos por portal:**")
        for pid, msg in errors.items():
            lines.append(f"- `{pid}`: {msg}")
    return "\n".join(lines)


def write_response(data: dict[str, Any]) -> None:
    print(json.dumps(data, default=str, ensure_ascii=False), flush=True)


def main() -> None:
    request: dict = {}
    try:
        request = json.loads(sys.stdin.read())
        request_id = request.get("request_id", "")
        args = request.get("arguments", {}) or {}

        query = str(args.get("query", "")).strip()
        if not query:
            write_response({
                "success": False, "request_id": request_id,
                "error": {"code": "MISSING_QUERY",
                          "message": "query is required (free-text search term)"},
            })
            return

        # Tags: accept string or list; default to the journalist interest set.
        raw_tags = args.get("tags")
        if raw_tags is None or (isinstance(raw_tags, (list, str)) and not raw_tags):
            tags: list[str] = list(DEFAULT_TAGS)
        elif isinstance(raw_tags, str):
            tags = [t.strip() for t in raw_tags.split(",") if t.strip()]
        elif isinstance(raw_tags, list):
            tags = [str(t).strip() for t in raw_tags if str(t).strip()]
        else:
            tags = list(DEFAULT_TAGS)

        # Portals: accept single string or list; default = all known.
        raw_portals = args.get("portal")
        if raw_portals is None:
            portal_filter: list[str] | None = None
        elif isinstance(raw_portals, str):
            portal_filter = [p.strip() for p in raw_portals.split(",") if p.strip()]
        elif isinstance(raw_portals, list):
            portal_filter = [str(p).strip() for p in raw_portals if str(p).strip()]
        else:
            portal_filter = None

        try:
            rows = int(args.get("rows", DEFAULT_ROWS))
        except (TypeError, ValueError):
            rows = DEFAULT_ROWS
        rows = max(1, min(rows, MAX_ROWS))

        try:
            timeout = int(args.get("timeout", DEFAULT_TIMEOUT))
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT
        timeout = max(1, min(timeout, 120))

        results, errors = search_all(portal_filter, query, tags, rows, timeout)

        if not results and errors.get("_config"):
            # User asked for portals that don't exist (or empty list).
            write_response({
                "success": False, "request_id": request_id,
                "error": {
                    "code": "UNKNOWN_PORTAL",
                    "message": errors["_config"],
                    "details": {"known_portals": list(PORTALS.keys())},
                },
            })
            return

        if not results and errors:
            # Every requested portal actually returned an error.
            write_response({
                "success": False, "request_id": request_id,
                "error": {
                    "code": "ALL_PORTALS_FAILED",
                    "message": "All queried CKAN portals failed",
                    "details": errors,
                },
            })
            return

        md = _format_markdown(query, results, errors)
        write_response({
            "success": True, "request_id": request_id,
            "content": [{"type": "text", "text": md}],
            "structured_content": {
                "source": "ckan",
                "query": query,
                "tags": tags,
                "portals_queried": portal_filter or list(PORTALS.keys()),
                "results": results,
                "count": len(results),
                "errors": errors,
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
