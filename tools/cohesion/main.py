#!/usr/bin/env python3
"""Cohesion data fetcher — Socrata SODA client for EU cohesion funds data.

Source: https://cohesiondata.ec.europa.eu (Socrata Open Data API / SODA)
Dataset: 7twf-r3rc — "2021-2027 Categorisation data raw annual timeseries
(Table 2)". This is the official raw transmission-of-data feed published by
DG REGIO / DG EMPL. Each row is a (programme × intervention field × support
form × territory × ...) combination with its planned / selected expenditure
for a given transmission cycle.

The schema exposed to the user is intentionally minimal: a single date
``date`` or a historical range ``from``/``to``. We map that to the dataset's
``tod_cycle`` column (YYYYMM transmission-of-data cycle), so e.g. the most
recent published cycle (202512) is reachable by passing any date in late
2025 / early 2026.
"""

import json
import os
import sys
from datetime import datetime
from typing import Any, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common.structured_logging import get_logger

logger = get_logger(__name__, "cohesion")

from common.http import request_with_retry, REQUESTS_AVAILABLE

COHESION_DATASET_ID = "7twf-r3rc"
COHESION_BASE_URL = (
    f"https://cohesiondata.ec.europa.eu/resource/{COHESION_DATASET_ID}.json"
)

# Fields we surface in the response. Kept short to stay readable in markdown
# while still giving the journalist enough to identify the programme and
# money involved.
COHESION_FIELDS = [
    "tod_cycle",
    "ms",
    "country",
    "fund",
    "cci",
    "cci_short",
    "priority",
    "pri_description",
    "objective",
    "obj_title",
    "region_cat",
    "interv_field_code",
    "interv_field_short",
    "support_form_short",
    "terr_delivery_short",
    "location_short",
    "total_eligible_cost",
    "total_eligible_expend",
    "selected_operat_n",
]

DEFAULT_LIMIT = 50
MAX_LIMIT = 1000
REQUEST_TIMEOUT = 30  # seconds


def _date_to_cycle(date_str: str) -> str:
    """Map a YYYY-MM-DD date to the YYYYMM transmission cycle containing it.

    The Table 2 dataset is published in December cycles only (per the
    dataset's own description: "It includes each December cycle of
    Transmissions of data (annual cumulative data)."). So a date in either
    half of a given year maps to that year's December cycle. If the
    December cycle for the requested year hasn't been published yet, the
    Socrata query simply returns zero rows — which is the correct
    behaviour.
    """
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"Invalid date '{date_str}', expected YYYY-MM-DD") from exc
    return f"{dt.year}12"


def _cycles_in_range(date_from: str, date_to: str) -> list[str]:
    """Enumerate every YYYYMM cycle whose end-of-period falls in [from, to].

    Table 2 is published in December cycles only, so we walk December of
    every year in the range, inclusive.
    """
    try:
        d_from = datetime.strptime(date_from, "%Y-%m-%d")
        d_to = datetime.strptime(date_to, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(
            f"Invalid date in range, expected YYYY-MM-DD: {exc}"
        ) from exc
    if d_to < d_from:
        raise ValueError("'to' must be on or after 'from'")
    cycles: list[str] = []
    for year in range(d_from.year, d_to.year + 1):
        cycle_end = datetime(year, 12, 28)
        if d_from <= cycle_end <= d_to:
            cycles.append(f"{year}12")
    return cycles


def _latest_published_cycles(limit: int = 3) -> list[str]:
    """Discover the most recent ``tod_cycle`` values present in the dataset.

    Uses a cheap Socrata ``$select=distinct tod_cycle`` query so we don't
    have to guess the current year. Returns cycles in descending order
    (newest first). Falls back to an empty list on any error so the caller
    can surface a clean "no data" message rather than a stack trace.
    """
    if not REQUESTS_AVAILABLE:
        return []
    try:
        resp = request_with_retry(
            "GET", COHESION_BASE_URL,
            params={
                "$select": "distinct tod_cycle",
                "$order": "tod_cycle DESC",
                "$limit": str(max(limit, 1)),
            },
            timeout=REQUEST_TIMEOUT,
            headers={"Accept": "application/json"},
        )
    except Exception:
        return []
    if resp.status_code != 200:
        return []
    try:
        rows = resp.json()
    except ValueError:
        return []
    return [
        str(r.get("tod_cycle", ""))
        for r in rows
        if isinstance(r, dict) and r.get("tod_cycle")
    ]


def _format_eur(value: Any) -> str:
    """Best-effort pretty-printing of a numeric value in EUR.

    The Socrata feed stores values as strings (sometimes with thousands
    separators, sometimes as plain numbers). Per the dataset's own
    description the figures are "annual cumulative" financial values per
    programme / intervention-field / support-form combination — denominated
    in EUR, not millions — so we suffix the unit rather than reformat.
    Accepts ``Any`` because Socrata JSON values are loosely typed.
    """
    if value is None or value == "":
        return "n/a"
    try:
        n = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return str(value)
    return f"{n:,.2f} €"


def fetch_cohesion(
    cycles: list[str],
    limit: int,
    offset: int,
    order: str = "total_eligible_cost DESC",
) -> tuple[Optional[list[dict[str, Any]]], Optional[str]]:
    """Query the Socrata endpoint for one or more transmission cycles.

    Returns ``(rows, None)`` on success or ``(None, error_message)`` on any
    failure (network, HTTP, JSON, oversized). Pagination is bounded by
    ``limit`` + ``offset``; callers are expected to pass sane values.
    """
    if not REQUESTS_AVAILABLE:
        return None, "requests library not available"
    if not cycles:
        return None, "no transmission cycles resolved for the requested date(s)"

    # Socrata IN (...) syntax. Quoting each value to keep things safe even
    # though cycles are digits only.
    quoted = ",".join(f"'{c}'" for c in cycles)
    where = f"tod_cycle in({quoted})"
    params = {
        "$where": where,
        "$limit": str(min(limit, MAX_LIMIT)),
        "$offset": str(max(offset, 0)),
        "$order": order,
    }
    try:
        resp = request_with_retry(
            "GET", COHESION_BASE_URL,
            params=params,
            timeout=REQUEST_TIMEOUT,
            headers={"Accept": "application/json", "User-Agent": "journalist-mcp/cohesion/1.0"},
        )
    except Exception as exc:
        msg = str(exc).lower()
        if "timeout" in msg or "timed" in msg:
            return None, "Cohesion API timed out"
        if "connection" in msg:
            return None, f"Cohesion API connection error: {exc}"
        return None, f"Cohesion API request failed: {exc}"

    if resp.status_code != 200:
        # Socrata error bodies are JSON like {"message": "...", "errorCode": "..."}.
        try:
            err_body = resp.json()
            detail = err_body.get("message") or err_body.get("errorCode") or resp.text[:200]
        except ValueError:
            detail = resp.text[:200]
        return None, f"Cohesion API returned HTTP {resp.status_code}: {detail}"

    try:
        rows = resp.json()
    except ValueError as exc:
        return None, f"Cohesion API returned invalid JSON: {exc}"

    if not isinstance(rows, list):
        return None, "Cohesion API returned unexpected payload (not a JSON array)"

    return rows, None


def write_response(data: dict[str, Any]) -> None:
    print(json.dumps(data, default=str), flush=True)


def _render_markdown(cycles: list[str], rows: list[dict[str, Any]]) -> str:
    if not rows:
        cycles_str = ", ".join(cycles)
        return (
            f"**Cohesion Data — Fondos UE**\n\n"
            f"Ningún resultado para ciclos tod_cycle={cycles_str}."
        )
    cycles_str = ", ".join(cycles)
    lines = [
        f"**Cohesion Data — Fondos UE (tod_cycle: {cycles_str})**",
        f"Resultados: {len(rows)} (mostrando hasta {len(rows)})\n",
    ]
    for i, r in enumerate(rows, 1):
        country = r.get("country") or r.get("ms") or "?"
        fund = r.get("fund") or "?"
        programme = r.get("cci_short") or r.get("cci") or "?"
        priority = r.get("pri_description") or r.get("priority") or "?"
        obj = r.get("obj_title") or r.get("objective") or "?"
        interv = r.get("interv_field_short") or r.get("interv_field_code") or "?"
        location = r.get("location_short") or "?"
        cost = _format_eur(r.get("total_eligible_cost"))
        expend = _format_eur(r.get("total_eligible_expend"))
        ops = r.get("selected_operat_n") or "?"
        lines.append(
            f"**{i}. {country} — {fund} — {programme}**"
        )
        lines.append(f"  Prioridad: {priority}")
        lines.append(f"  Objetivo: {obj}")
        lines.append(f"  Intervención: {interv}")
        lines.append(f"  Localización: {location}")
        lines.append(f"  Coste elegible: {cost} | Ejecutado: {expend} | Operaciones: {ops}\n")
    return "\n".join(lines)


def main() -> None:
    request: dict = {}
    try:
        raw = sys.stdin.read()
        request = json.loads(raw) if raw.strip() else {}
        request_id = request.get("request_id", "")
        args = request.get("arguments", {}) or {}
        # ``from`` is a Python keyword, so the JSON key is "from" but the
        # local variable can't be. Track which fields the user actually
        # provided in the input so we can detect genuine conflicts
        # (date+range) without misfiring when only one mode is used.
        date_arg = args.get("date")
        from_arg = args.get("from")
        to_arg = args.get("to")
        date = str(date_arg).strip() if date_arg is not None else ""
        date_from = str(from_arg).strip() if from_arg is not None else ""
        date_to = str(to_arg).strip() if to_arg is not None else ""

        has_date = bool(date)
        has_range = bool(date_from) or bool(date_to)
        if has_date and has_range:
            write_response({
                "success": False, "request_id": request_id,
                "error": {
                    "code": "INVALID_INPUT",
                    "message": "Use either 'date' or 'from'+'to', not both",
                },
            })
            return
        if bool(date_from) != bool(date_to):
            write_response({
                "success": False, "request_id": request_id,
                "error": {
                    "code": "MISSING_RANGE",
                    "message": "'from' and 'to' must be provided together",
                },
            })
            return

        try:
            if date:
                cycles = [_date_to_cycle(date)]
            elif date_from and date_to:
                cycles = _cycles_in_range(date_from, date_to)
            else:
                # No input — probe the API for the most recent published
                # cycle so a journalist without a specific date still sees
                # fresh data. Returns at most the top 3, newest first.
                cycles = _latest_published_cycles(limit=3)
        except ValueError as exc:
            write_response({
                "success": False, "request_id": request_id,
                "error": {"code": "INVALID_DATE", "message": str(exc)},
            })
            return

        if not cycles:
            write_response({
                "success": False, "request_id": request_id,
                "error": {
                    "code": "EMPTY_RANGE",
                    "message": "Resolved date range contains no transmission cycles",
                },
            })
            return

        rows, error = fetch_cohesion(cycles, limit=DEFAULT_LIMIT, offset=0)
        if error:
            logger.error("Cohesion fetch failed", extra_data={"error": error, "cycles": cycles})
            write_response({
                "success": False, "request_id": request_id,
                "error": {"code": "FETCH_FAILED", "message": error},
            })
            return
        if rows is None:  # defensive — fetch_cohesion only returns None with error
            write_response({
                "success": False, "request_id": request_id,
                "error": {"code": "FETCH_FAILED", "message": "no rows returned"},
            })
            return

        text = _render_markdown(cycles, rows)
        # Only the fields we care about, in stable order.
        projected = [
            {k: r.get(k, "") for k in COHESION_FIELDS}
            for r in rows
        ]
        write_response({
            "success": True,
            "request_id": request_id,
            "content": [{"type": "text", "text": text}],
            "structured_content": {
                "source": "cohesion",
                "dataset_id": COHESION_DATASET_ID,
                "cycles": cycles,
                "count": len(projected),
                "results": projected,
            },
        })
    except json.JSONDecodeError:
        write_response({
            "success": False, "request_id": "",
            "error": {"code": "INVALID_JSON", "message": "Failed to parse JSON"},
        })
    except Exception as exc:  # noqa: BLE001 — top-level guard, must never crash
        logger.error("Unhandled exception", extra_data={"error": str(exc)})
        write_response({
            "success": False,
            "request_id": request.get("request_id", "") if isinstance(request, dict) else "",
            "error": {"code": "EXECUTION_FAILED", "message": str(exc)},
        })


if __name__ == "__main__":
    main()
