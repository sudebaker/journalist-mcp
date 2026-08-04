#!/usr/bin/env python3
"""fts_fetch — FTS (Financial Tracking Service) humanitarian funding data.

FTS es el servicio de seguimiento financiero de OCHA para flujos de
financiación humanitaria. Los datos están publicados a través de la
plataforma HDX (Humanitarian Data Exchange) en data.humdata.org como
ficheros CSV. Este tool los descarga vía la API CKAN de HDX, aplica los
filtros del usuario (fecha de reporte, rango de años, texto libre, país)
y devuelve los resultados como SubprocessResponse JSON por STDOUT.

Patrón de implementación: tools/ted_search/main.py.
"""

import csv
import io
import json
import os
import re
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common.structured_logging import get_logger

logger = get_logger(__name__, "fts_fetch")

try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False

HDX_API_URL = "https://data.humdata.org/api/3/action"
CSV_DOWNLOAD_TIMEOUT = 120
CSV_MAX_BYTES = 100 * 1024 * 1024  # 100 MB hard cap

GLOBAL_DATASET = "global-requirements-and-funding-data"
COUNTRY_DATASET_TEMPLATE = "{iso3}-requirements-and-funding-data"

# Resource filename patterns to resource purpose.
# The order matters: more specific patterns are tried first.
RESOURCE_PATTERNS: List[Tuple[str, str]] = [
    (r"fts_requirements_funding_(?P<scope>{scope})\.csv$", "requirements_funding"),
    (r"fts_incoming_funding_(?P<scope>{scope})\.csv$", "incoming_funding"),
    (r"fts_internal_funding_(?P<scope>{scope})\.csv$", "internal_funding"),
    (r"fts_outgoing_funding_(?P<scope>{scope})\.csv$", "outgoing_funding"),
]


def fetch_dataset(name: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Fetch a dataset by name from the HDX CKAN API.

    Returns (dataset, error). Dataset has 'resources' list with URLs and
    metadata needed to pick the right CSV.
    """
    if not REQUESTS_AVAILABLE:
        return None, "requests library not available"
    url = f"{HDX_API_URL}/package_show"
    try:
        resp = requests.get(url, params={"id": name}, timeout=30,
                            headers={"Accept": "application/json",
                                     "User-Agent": "journalist-mcp/fts_fetch"})
    except requests.exceptions.Timeout:
        return None, "HDX API timed out"
    except requests.exceptions.RequestException as e:
        return None, f"HDX API request failed: {e}"

    if resp.status_code == 404:
        return None, f"HDX dataset not found: {name}"
    if resp.status_code != 200:
        return None, f"HDX API returned HTTP {resp.status_code}"

    try:
        data = resp.json()
    except ValueError:
        return None, "HDX API returned non-JSON response"

    if not data.get("success"):
        return None, f"HDX API returned success=false for {name}"
    return data.get("result"), None


def pick_resource_url(dataset: Dict[str, Any], scope: str,
                      kind: str) -> Optional[str]:
    """Pick a CSV resource URL matching a kind ('requirements_funding',
    'incoming_funding', etc.) and scope ('global' or ISO3 code)."""
    # Country resources use lowercase ISO3 in their filename; 'global'
    # stays 'global'. We match case-insensitively to be tolerant.
    scope_re = re.escape(scope)
    for fname_pat, fname_kind in RESOURCE_PATTERNS:
        if fname_kind != kind:
            continue
        pattern = fname_pat.format(scope=scope_re)
        cre = re.compile(pattern, re.IGNORECASE)
        for r in dataset.get("resources", []):
            name = r.get("name") or ""
            fmt = (r.get("format") or "").upper()
            if fmt != "CSV":
                continue
            if cre.search(name):
                return r.get("url")
    return None


def download_csv(url: str) -> Tuple[Optional[List[Dict[str, str]]], Optional[str]]:
    """Stream a CSV from a URL and return its rows as a list of dicts."""
    if not REQUESTS_AVAILABLE:
        return None, "requests library not available"
    try:
        resp = requests.get(url, timeout=CSV_DOWNLOAD_TIMEOUT, stream=True,
                            headers={"User-Agent": "journalist-mcp/fts_fetch"})
    except requests.exceptions.Timeout:
        return None, "CSV download timed out"
    except requests.exceptions.RequestException as e:
        return None, f"CSV download failed: {e}"

    if resp.status_code != 200:
        return None, f"CSV download returned HTTP {resp.status_code}"

    buf = io.BytesIO()
    bytes_read = 0
    for chunk in resp.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        bytes_read += len(chunk)
        if bytes_read > CSV_MAX_BYTES:
            return None, f"CSV exceeds {CSV_MAX_BYTES} bytes"
        buf.write(chunk)
    try:
        text = buf.getvalue().decode("utf-8-sig")
    except UnicodeDecodeError:
        text = buf.getvalue().decode("latin-1", errors="replace")

    try:
        reader = csv.DictReader(io.StringIO(text))
        rows = list(reader)
    except csv.Error as e:
        return None, f"CSV parse failed: {e}"
    return rows, None


def _coerce_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    s = str(value).strip().replace(",", "")
    if not s:
        return None
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return None


def _matches_query(row: Dict[str, str], needle: str) -> bool:
    """Free-text search across the most informative columns of a row."""
    haystack_cols = (
        "description", "srcOrganization", "destOrganization",
        "destPlan", "destProject", "srcLocations", "destLocations",
        "refCode",
    )
    needle_lc = needle.lower()
    for col in haystack_cols:
        v = row.get(col)
        if v and needle_lc in v.lower():
            return True
    return False


def filter_rows(rows: List[Dict[str, str]], *, year_from: Optional[int],
                year_to: Optional[int], date_from: Optional[str],
                query: Optional[str], country_iso3: Optional[str],
                kind: str) -> List[Dict[str, str]]:
    """Apply user filters to a row set."""
    out: List[Dict[str, str]] = []
    q = query.strip() if query else ""
    cf = (country_iso3 or "").upper().strip() or None
    for row in rows:
        # Year filter — applicable to both kinds via budgetYear/year
        if year_from is not None or year_to is not None:
            year_val = _coerce_int(row.get("budgetYear") or row.get("year"))
            if year_val is None:
                continue
            if year_from is not None and year_val < year_from:
                continue
            if year_to is not None and year_val > year_to:
                continue
        # Date-from filter — only meaningful on flow rows
        if date_from:
            row_date = (row.get("date") or "")[:10]
            if not row_date or row_date < date_from:
                continue
        # Country filter — apply when scope is global and user gave a country
        if cf:
            if kind == "requirements_funding":
                cc = (row.get("countryCode") or "").upper()
                if cc != cf:
                    continue
            else:
                # Flow rows: match if the country appears in either side
                src = (row.get("srcLocations") or "").upper()
                dst = (row.get("destLocations") or "").upper()
                if cf not in (src, dst):
                    continue
        # Free-text query
        if q and not _matches_query(row, q):
            continue
        out.append(row)
    return out


def format_requirements(results: List[Dict[str, str]],
                        country_iso3: Optional[str], query: str) -> str:
    """Markdown view of requirements/funding rows."""
    title_scope = country_iso3.upper() if country_iso3 else "Global"
    lines = [f"**FTS — Financiación humanitaria ({title_scope})**\n"]
    if query:
        lines.append(f"_Búsqueda: \"{query}\"_")
        lines.append("")
    if not results:
        lines.append("No se encontraron resultados.")
        return "\n".join(lines)
    # Aggregate by year
    by_year: Dict[str, Dict[str, float]] = {}
    for r in results:
        y = r.get("year") or "?"
        agg = by_year.setdefault(y, {"requirements": 0.0, "funding": 0.0,
                                      "count": 0})
        req = _coerce_int(r.get("requirements")) or 0
        fnd = _coerce_int(r.get("funding")) or 0
        agg["requirements"] += req
        agg["funding"] += fnd
        agg["count"] += 1
    for y in sorted(by_year.keys(), reverse=True):
        agg = by_year[y]
        req = agg["requirements"]
        fnd = agg["funding"]
        pct = (fnd / req * 100) if req else 0.0
        lines.append(f"**Año {y}** — {int(agg['count'])} planes")
        lines.append(f"  - Requerido: ${req:,.0f}")
        lines.append(f"  - Financiado: ${fnd:,.0f} ({pct:.1f}%)")
    return "\n".join(lines)


def format_flows(results: List[Dict[str, str]], country_iso3: Optional[str],
                 query: str, kind_label: str) -> str:
    """Markdown view of incoming/internal/outgoing flow rows."""
    title_scope = country_iso3.upper() if country_iso3 else "Global"
    lines = [f"**FTS — Flujos de financiación {kind_label} ({title_scope})**\n"]
    if query:
        lines.append(f"_Búsqueda: \"{query}\"_")
        lines.append("")
    if not results:
        lines.append("No se encontraron resultados.")
        return "\n".join(lines)
    for i, r in enumerate(results, 1):
        date = (r.get("date") or "")[:10]
        amount = _coerce_int(r.get("amountUSD")) or 0
        src = r.get("srcOrganization") or ""
        dst = r.get("destOrganization") or ""
        plan = r.get("destPlan") or ""
        status = r.get("status") or ""
        flow_type = r.get("flowType") or ""
        ref = r.get("refCode") or ""
        lines.append(f"**{i}. {date} — ${amount:,.0f} USD**")
        lines.append(f"  Origen: {src}")
        if dst and dst != src:
            lines.append(f"  Destino: {dst}")
        if plan:
            lines.append(f"  Plan: {plan}")
        meta = " | ".join(filter(None, [flow_type, status, ref]))
        if meta:
            lines.append(f"  {meta}")
    return "\n".join(lines)


def write_response(data: Dict[str, Any]) -> None:
    print(json.dumps(data, default=str, ensure_ascii=False), flush=True)


def _validate_args(args: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[str]]:
    """Validate and coerce input arguments. Returns (clean, error_msg)."""
    clean: Dict[str, Any] = {}
    # country (optional, ISO3)
    country = args.get("country", "")
    if country:
        cc = str(country).strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", cc):
            return clean, "country must be a 3-letter ISO code (e.g. AFG)"
        clean["country"] = cc
    else:
        clean["country"] = None

    # from / to (years, optional, integers)
    for k_in, k_out in (("from", "year_from"), ("to", "year_to")):
        v = args.get(k_in)
        if v in (None, ""):
            clean[k_out] = None
            continue
        try:
            year = int(str(v).strip())
        except (ValueError, TypeError):
            return clean, f"{k_in} must be a year (integer, e.g. 2024)"
        if year < 1900 or year > 2100:
            return clean, f"{k_in} must be a plausible year"
        clean[k_out] = year

    if (clean["year_from"] is not None and clean["year_to"] is not None
            and clean["year_from"] > clean["year_to"]):
        return clean, "from must be <= to"

    # date (optional, YYYY-MM-DD)
    date = args.get("date", "")
    if date:
        s = str(date).strip()
        try:
            datetime.strptime(s, "%Y-%m-%d")
        except ValueError:
            return clean, "date must be YYYY-MM-DD"
        clean["date"] = s
    else:
        clean["date"] = None

    # query (optional, free-text)
    q = args.get("query", "")
    clean["query"] = str(q).strip() if q else ""

    return clean, None


def _resolve_dataset_name(country: Optional[str]) -> str:
    if country:
        return COUNTRY_DATASET_TEMPLATE.format(iso3=country.lower())
    return GLOBAL_DATASET


def _kind_label(kind: str) -> str:
    return {
        "requirements_funding": "Requisitos y financiación",
        "incoming_funding": "entrantes",
        "internal_funding": "internos",
        "outgoing_funding": "salientes",
    }.get(kind, kind)


def main() -> None:
    request: Dict[str, Any] = {}
    try:
        request = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        write_response({"success": False, "request_id": "",
                        "error": {"code": "INVALID_JSON",
                                  "message": "Failed to parse JSON"}})
        return

    request_id = request.get("request_id", "")
    args = request.get("arguments") or {}

    clean, err = _validate_args(args)
    if err:
        write_response({"success": False, "request_id": request_id,
                        "error": {"code": "INVALID_ARGUMENT", "message": err}})
        return

    if not any([clean["country"], clean["year_from"], clean["year_to"],
                clean["date"], clean["query"]]):
        write_response({"success": False, "request_id": request_id,
                        "error": {"code": "MISSING_FILTER",
                                  "message": ("Provide at least one of: "
                                              "country, from, to, date, query")}})
        return

    # Decide resource kind. Requirements/funding rows support country+year
    # filters; incoming flows support date+query. We pick the more useful
    # one based on which filter the user actually provided.
    if clean["query"] or clean["date"]:
        kind = "incoming_funding"
    else:
        kind = "requirements_funding"

    scope = clean["country"] or "global"
    dataset_name = _resolve_dataset_name(clean["country"])

    dataset, err = fetch_dataset(dataset_name)
    if err:
        write_response({"success": False, "request_id": request_id,
                        "error": {"code": "HDX_FETCH_FAILED", "message": err}})
        return

    url = pick_resource_url(dataset, scope, kind)
    if not url:
        # Fallback: if a country dataset lacks incoming flows (older
        # datasets), retry with requirements/funding.
        fallback = ("requirements_funding" if kind == "incoming_funding"
                    else "incoming_funding")
        url = pick_resource_url(dataset, scope, fallback)
        if not url:
            write_response({"success": False, "request_id": request_id,
                            "error": {"code": "RESOURCE_NOT_FOUND",
                                      "message": (f"No matching CSV resource "
                                                  f"found in dataset "
                                                  f"{dataset_name}")}})
            return
        kind = fallback

    rows, err = download_csv(url)
    if err:
        write_response({"success": False, "request_id": request_id,
                        "error": {"code": "CSV_DOWNLOAD_FAILED",
                                  "message": err}})
        return

    results = filter_rows(
        rows,
        year_from=clean["year_from"],
        year_to=clean["year_to"],
        date_from=clean["date"],
        query=clean["query"],
        country_iso3=clean["country"] if scope == "global" else None,
        kind=kind,
    )

    count = len(results)
    if kind == "requirements_funding":
        body = format_requirements(results, clean["country"], clean["query"])
    else:
        body = format_flows(results, clean["country"], clean["query"],
                            _kind_label(kind))

    write_response({
        "success": True,
        "request_id": request_id,
        "content": [{"type": "text", "text": body}],
        "structured_content": {
            "source": "fts",
            "dataset": dataset_name,
            "resource_kind": kind,
            "country": clean["country"],
            "year_from": clean["year_from"],
            "year_to": clean["year_to"],
            "date_from": clean["date"],
            "query": clean["query"],
            "results": results,
            "count": count,
        },
    })


if __name__ == "__main__":
    main()
