#!/usr/bin/env python3
"""BDNS fetch — aggregate Spanish subsidies data from the BDNS public API.

Iterates over the 5 BDNS categories (convocatorias, concesiones, planesestrategicos,
minimis, sanciones) for a given date / date range, paginates within a per-endpoint
result cap, and emits a SubprocessResponse aggregating the per-category hits.

The API returns Spring-style page objects:
    {"content": [...], "totalElements": N, "totalPages": M, "pageable": {...}}

Date params (fechaDesde / fechaHasta) require the format dd/MM/yyyy.
"""

import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common.structured_logging import get_logger

logger = get_logger(__name__, "bdns_fetch")

try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False


@dataclass(frozen=True)
class DateRange:
    fecha_desde: str
    fecha_hasta: str

BDNS_API_BASE = "https://www.infosubvenciones.es/bdnstrans/api"

# Per-endpoint configuration: path + which fields to surface in the response.
# "date_field" is informational — we always send fechaDesde/fechaHasta which the
# API maps to the relevant per-endpoint date (fechaRecepcion, fechaConcesion, etc.).
ENDPOINTS = [
    {
        "key": "convocatorias",
        "path": "convocatorias/busqueda",
        "label": "Convocatorias de subvenciones",
        "id_field": "id",
        "fields": ["numeroConvocatoria", "descripcion", "fechaRecepcion",
                   "nivel1", "nivel2", "nivel3", "mrr"],
    },
    {
        "key": "concesiones",
        "path": "concesiones/busqueda",
        "label": "Concesiones otorgadas",
        "id_field": "id",
        "fields": ["codConcesion", "fechaConcesion", "beneficiario", "importe",
                   "instrumento", "convocatoria", "nivel1", "nivel2", "nivel3"],
    },
    {
        "key": "planesestrategicos",
        "path": "planesestrategicos/busqueda",
        "label": "Planes estratégicos",
        "id_field": "id",
        "fields": ["descripcion", "tipoPlan", "vigenciaDesde", "vigenciaHasta", "ambitos"],
    },
    {
        "key": "minimis",
        "path": "minimis/busqueda",
        "label": "Ayudas minimis",
        "id_field": "idConcesion",
        "fields": ["numeroConvocatoria", "convocante", "reglamento", "fechaConcesion",
                   "beneficiario", "sectorActividad", "ayudaEquivalente"],
    },
    {
        "key": "sanciones",
        "path": "sanciones/busqueda",
        "label": "Sanciones",
        "id_field": "numeroConvocatoria",
        "fields": ["numeroConvocatoria", "sancionado", "fechaSancion", "importeMulta",
                   "infraccion", "inicioInhabilitacion", "finInhabilitacion"],
    },
]

# Hard caps — this is a discovery tool, not a bulk exporter.
MAX_PAGES_PER_ENDPOINT = 2
MAX_RESULTS_PER_ENDPOINT = 50
PER_REQUEST_TIMEOUT = 20


def to_api_date(iso_date: str) -> Optional[str]:
    """Convert YYYY-MM-DD to dd/MM/yyyy (the format BDNS API expects)."""
    if not iso_date:
        return None
    try:
        return datetime.strptime(iso_date, "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        return None


def resolve_date_range(args: dict[str, Any]) -> tuple[Optional[str], Optional[DateRange]]:
    """Apply the date / from / to precedence rules from tool.yaml.

    Returns (error, DateRange|None).
    """
    date = str(args.get("date", "")).strip()
    from_d = str(args.get("from", "")).strip()
    to_d = str(args.get("to", "")).strip()

    has_date = bool(date)
    has_range = bool(from_d or to_d)

    if has_date and has_range:
        return ("'date' es mutuamente excluyente con 'from'/'to'", None)

    if has_range and (not from_d or not to_d):
        return ("'from' y 'to' deben proporcionarse juntos", None)

    if has_date:
        api_date = to_api_date(date)
        if not api_date:
            return (f"'date' inválido: {date!r} (esperado YYYY-MM-DD)", None)
        return (None, DateRange(api_date, api_date))

    if has_range:
        desde = to_api_date(from_d)
        hasta = to_api_date(to_d)
        if not desde or not hasta:
            return (f"Rango inválido: from={from_d!r}, to={to_d!r}", None)
        if desde > hasta:
            return ("'from' debe ser <= 'to'", None)
        return (None, DateRange(desde, hasta))

    # No date provided — default to today.
    today = datetime.now().strftime("%d/%m/%Y")
    return (None, DateRange(today, today))


def fetch_endpoint(endpoint: dict[str, Any], fecha_desde: str, fecha_hasta: str) -> dict[str, Any]:
    """Query one BDNS endpoint, paginated, with hard caps.

    Returns a dict:
        {"key": ..., "label": ..., "totalElements": N, "results": [...],
         "pages_fetched": K, "truncated": bool, "error": str|None, "status_code": int|None}
    """
    out: dict[str, Any] = {
        "key": endpoint["key"],
        "label": endpoint["label"],
        "totalElements": 0,
        "results": [],
        "pages_fetched": 0,
        "truncated": False,
        "error": None,
        "status_code": None,
    }
    if not REQUESTS_AVAILABLE:
        out["error"] = "requests library not available"
        return out

    url = f"{BDNS_API_BASE}/{endpoint['path']}"
    collected: list[dict[str, Any]] = []

    for page in range(MAX_PAGES_PER_ENDPOINT):
        params = {
            "fechaDesde": fecha_desde,
            "fechaHasta": fecha_hasta,
            "page": page,
            "pageSize": min(MAX_RESULTS_PER_ENDPOINT // MAX_PAGES_PER_ENDPOINT, 50),
        }
        try:
            resp = requests.get(  # type: ignore[name-defined]
                url, params=params, timeout=PER_REQUEST_TIMEOUT,
                headers={"Accept": "application/json"},
            )
        except requests.exceptions.Timeout:  # type: ignore[name-defined]
            out["error"] = f"timeout after {PER_REQUEST_TIMEOUT}s"
            break
        except requests.exceptions.RequestException as e:  # type: ignore[name-defined]
            out["error"] = f"request failed: {e}"
            break

        out["status_code"] = resp.status_code
        if resp.status_code != 200:
            # Capture API error body for diagnostics, but don't fail the whole tool.
            try:
                err_body = resp.json()
                out["error"] = f"HTTP {resp.status_code}: {err_body.get('codigo', '')}"
            except ValueError:
                out["error"] = f"HTTP {resp.status_code}"
            break

        try:
            data = resp.json()
        except ValueError as e:
            out["error"] = f"invalid JSON: {e}"
            break

        out["pages_fetched"] += 1
        if page == 0:
            out["totalElements"] = int(data.get("totalElements", 0) or 0)

        content = data.get("content") or []
        for item in content:
            if len(collected) >= MAX_RESULTS_PER_ENDPOINT:
                break
            slim = {"id": item.get(endpoint["id_field"])}
            for f in endpoint["fields"]:
                val = item.get(f)
                if val is not None:
                    slim[f] = val
            collected.append(slim)

        # Stop conditions: short page → no more data, or hit result cap.
        if len(content) < params["pageSize"]:
            break
        if len(collected) >= MAX_RESULTS_PER_ENDPOINT:
            out["truncated"] = True
            break

    out["results"] = collected
    return out


def format_markdown(per_endpoint: list[dict[str, Any]],
                    fecha_desde: str, fecha_hasta: str) -> str:
    """Build a human-readable markdown summary."""
    is_single_day = fecha_desde == fecha_hasta
    if is_single_day:
        header = f"**BDNS — Ayudas y subvenciones públicas ({fecha_desde})**\n"
    else:
        header = f"**BDNS — Ayudas y subvenciones públicas ({fecha_desde} → {fecha_hasta})**\n"

    total_hits = sum(e["totalElements"] for e in per_endpoint)
    total_collected = sum(len(e["results"]) for e in per_endpoint)
    errors = [e for e in per_endpoint if e["error"]]

    lines = [header, f"Total registros en BDNS (filtrado): {total_hits:,}".replace(",", "."),
             f"Muestras recogidas: {total_collected} (cap {MAX_RESULTS_PER_ENDPOINT}/categoría)\n"]

    for ep in per_endpoint:
        lines.append(f"### {ep['label']} — {ep['totalElements']:,} registros".replace(",", "."))
        if ep["error"]:
            lines.append(f"⚠️  Error: {ep['error']}\n")
            continue
        if not ep["results"]:
            lines.append("Sin resultados para el rango.\n")
            continue
        for i, r in enumerate(ep["results"][:10], 1):
            keys = [k for k in r if k != "id"]
            summary = " | ".join(f"{k}={r[k]}" for k in keys[:4] if r.get(k) not in (None, ""))
            if not summary:
                summary = f"id={r.get('id', '')}"
            lines.append(f"  {i}. {summary[:300]}")
        if len(ep["results"]) > 10:
            lines.append(f"  … y {len(ep['results']) - 10} más (truncado en respuesta estructurada)")
        if ep["truncated"]:
            lines.append(f"  ⚠️  Resultados truncados — hay {ep['totalElements']:,} totales en BDNS".replace(",", "."))
        lines.append("")

    if errors:
        lines.append(f"**{len(errors)} categoría(s) con error** — ver detalles arriba.")

    return "\n".join(lines)


def write_response(data: dict[str, Any]) -> None:
    print(json.dumps(data, default=str, ensure_ascii=False), flush=True)


def main() -> None:
    request: dict = {}
    try:
        request = json.loads(sys.stdin.read())
        request_id = request.get("request_id", "")
        args = request.get("arguments", {})

        err, date_range = resolve_date_range(args)
        if err:
            write_response({"success": False, "request_id": request_id,
                            "error": {"code": "INVALID_INPUT", "message": err}})
            return
        assert date_range is not None  # type-narrowing for pyright; logically guaranteed

        logger.info("BDNS fetch starting",
                    extra_data={"fechaDesde": date_range.fecha_desde,
                                "fechaHasta": date_range.fecha_hasta})

        per_endpoint: list[dict[str, Any]] = []
        for ep in ENDPOINTS:
            result = fetch_endpoint(ep, date_range.fecha_desde, date_range.fecha_hasta)
            per_endpoint.append(result)
            logger.info("BDNS endpoint fetched",
                        extra_data={"endpoint": ep["key"],
                                    "totalElements": result["totalElements"],
                                    "collected": len(result["results"]),
                                    "error": result["error"]})

        markdown = format_markdown(per_endpoint,
                                   date_range.fecha_desde, date_range.fecha_hasta)
        any_error = any(e["error"] for e in per_endpoint)
        total_collected = sum(len(e["results"]) for e in per_endpoint)

        write_response({
            "success": not any_error,
            "request_id": request_id,
            "content": [{"type": "text", "text": markdown}],
            "structured_content": {
                "source": "bdns",
                "fechaDesde": date_range.fecha_desde,
                "fechaHasta": date_range.fecha_hasta,
                "endpoints": per_endpoint,
                "total_collected": total_collected,
            },
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
