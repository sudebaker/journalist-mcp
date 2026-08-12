#!/usr/bin/env python3
"""boe_fetch — Descarga sumarios del BOE (Boletín Oficial del Estado).

Lee una petición JSON por STDIN con los campos definidos en ``tool.yaml`` y
devuelve un ``SubprocessResponse`` (success/content/structured_content) por
STDOUT. Sigue el mismo patrón que ``tools/ted_search/main.py`` y
``tools/boe_search/main.py``.

Modos de uso (uno u otro):
  - ``date``  → una sola fecha (YYYY-MM-DD)
  - ``from`` + ``to`` → rango histórico de fechas (YYYY-MM-DD / YYYY-MM-DD)

Campos opcionales:
  - ``kind``      → "sumario" (default) o "diario"
  - ``workers``   → nº de workers concurrentes para el rango (default 4)
  - ``rate_limit``→ requests por minuto (rate limit básico client-side)
"""

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common.structured_logging import get_logger  # noqa: E402

logger = get_logger(__name__, "boe_fetch")

from common.http import request_with_retry, REQUESTS_AVAILABLE

BOE_API_BASE = "https://www.boe.es/datosabiertos/api"
DEFAULT_WORKERS = 4
DEFAULT_RATE_LIMIT = 30  # req/min — conservador; BOE no documenta límite duro
DEFAULT_KIND = "sumario"  # "sumario" | "diario"
MAX_RANGE_DAYS = 365  # tope para evitar rangos absurdos


def _to_boe_date(s: str) -> str:
    """Convierte YYYY-MM-DD → YYYYMMDD. Lanza ValueError si el formato es inválido."""
    return datetime.strptime(s, "%Y-%m-%d").strftime("%Y%m%d")


def _parse_iso_date(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d")


def _date_range(from_str: str, to_str: str) -> list[str]:
    """Genera la lista de fechas en formato YYYYMMDD entre from y to (inclusive)."""
    start = _parse_iso_date(from_str)
    end = _parse_iso_date(to_str)
    if end < start:
        start, end = end, start
    days = (end - start).days + 1
    if days > MAX_RANGE_DAYS:
        raise ValueError(f"date range too large: {days} days (max {MAX_RANGE_DAYS})")
    out: list[str] = []
    current = start
    while current <= end:
        out.append(current.strftime("%Y%m%d"))
        current += timedelta(days=1)
    return out


def _endpoint_url(kind: str, boe_date: str) -> str:
    return f"{BOE_API_BASE}/boe/{kind}/{boe_date}"


def fetch_day(kind: str, boe_date: str, rate_limiter: "RateLimiter | None" = None) -> dict:
    """Descarga el sumario/diario de un día. Devuelve dict con metadatos y ``items``."""
    if not REQUESTS_AVAILABLE:
        return {
            "date": boe_date,
            "kind": kind,
            "url": _endpoint_url(kind, boe_date),
            "status": None,
            "error": "requests library not available",
            "items": [],
        }
    if rate_limiter is not None:
        rate_limiter.wait()
    url = _endpoint_url(kind, boe_date)
    record: dict[str, Any] = {
        "date": boe_date,
        "kind": kind,
        "url": url,
        "status": None,
        "error": None,
        "items": [],
    }
    try:
        resp = request_with_retry("GET", url, headers={"Accept": "application/json"}, timeout=20)
        record["status"] = resp.status_code
        if resp.status_code != 200:
            record["error"] = f"HTTP {resp.status_code}"
            return record
        data = resp.json()
        # Estructura real del sumario BOE:
        #   data.sumario.diario[].seccion[].departamento[].epigrafe[].item
        # donde ``item`` final es dict (una disposición) o list (varias).
        # Aplanamos a una lista de items con contexto (sección, departamento, epígrafe).
        record["items"] = _flatten_sumario(data)
        record["count"] = len(record["items"])
    except json.JSONDecodeError:
        record["error"] = "invalid json response"
    except Exception as e:  # noqa: BLE001
        msg = str(e).lower()
        if "timeout" in msg or "timed" in msg:
            record["error"] = "timeout"
        else:
            record["error"] = f"request error: {e}"
    return record


class RateLimiter:
    """Client-side rate limiter muy simple (token-bucket por minuto)."""

    def __init__(self, per_minute: int):
        self.min_interval = 60.0 / max(per_minute, 1)
        self._last = 0.0

    def wait(self) -> None:
        elapsed = time.monotonic() - self._last
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last = time.monotonic()


def _flatten_sumario(data: dict) -> list[dict]:
    """Aplana la estructura anidada del sumario BOE a una lista de items con contexto.

    Recorre ``data.sumario.diario[].seccion[].departamento[].epigrafe[].item``
    y devuelve cada item con los campos ``seccion``, ``departamento`` y
    ``epigrafe`` añadidos para no perder el contexto.
    """
    flat: list[dict] = []
    sumario = (data.get("data") or {}).get("sumario") or {}
    diario = sumario.get("diario") or []
    if isinstance(diario, dict):
        diario = [diario]
    for day in diario:
        secciones = day.get("seccion") or []
        if isinstance(secciones, dict):
            secciones = [secciones]
        for sec in secciones:
            sec_nombre = sec.get("nombre") or sec.get("codigo")
            departamentos = sec.get("departamento") or []
            if isinstance(departamentos, dict):
                departamentos = [departamentos]
            for depto in departamentos:
                depto_nombre = depto.get("nombre") or depto.get("codigo")
                epigrafes = depto.get("epigrafe") or []
                if isinstance(epigrafes, dict):
                    epigrafes = [epigrafes]
                for epi in epigrafes:
                    epi_nombre = epi.get("nombre")
                    leaves = epi.get("item")
                    if leaves is None:
                        continue
                    if isinstance(leaves, dict):
                        leaves = [leaves]
                    for leaf in leaves:
                        enriched = dict(leaf)
                        enriched["_seccion"] = sec_nombre
                        enriched["_departamento"] = depto_nombre
                        enriched["_epigrafe"] = epi_nombre
                        flat.append(enriched)
    return flat


def _summarize_item(item: Any) -> dict:
    """Extrae campos representativos de un item del BOE para evitar payloads enormes."""
    if not isinstance(item, dict):
        return {"raw": str(item)[:300]}
    out = {
        "identificador": item.get("identificador") or item.get("id"),
        "titulo": item.get("titulo") or item.get("title"),
        "url_html": (item.get("url_html") if isinstance(item.get("url_html"), str)
                     else (item.get("url_html") or {}).get("texto") if isinstance(item.get("url_html"), dict)
                     else None),
        "url_pdf": (item.get("url_pdf") or {}).get("texto") if isinstance(item.get("url_pdf"), dict)
                   else item.get("url_pdf") if isinstance(item.get("url_pdf"), str)
                   else None,
        "departamento": item.get("_departamento") or item.get("departamento"),
        "seccion": item.get("_seccion") or item.get("rango"),
        "epigrafe": item.get("_epigrafe"),
    }
    # Mantenemos sólo campos con valor
    return {k: v for k, v in out.items() if v not in (None, "")}


def fetch_single(kind: str, date_str: str, rate_limit: int) -> tuple[dict | None, str | None]:
    if not REQUESTS_AVAILABLE:
        return None, "requests library not available"
    if kind not in ("sumario", "diario"):
        return None, f"invalid kind: {kind!r} (expected 'sumario' or 'diario')"
    try:
        boe_date = _to_boe_date(date_str)
    except ValueError:
        return None, f"invalid date format: {date_str!r} (expected YYYY-MM-DD)"
    limiter = RateLimiter(rate_limit) if rate_limit else None
    record = fetch_day(kind, boe_date, limiter)
    if record["error"]:
        return None, f"BOE fetch failed for {boe_date}: {record['error']}"
    summary = {
        "kind": kind,
        "date": boe_date,
        "url": record["url"],
        "count": record.get("count", 0),
        "items": [_summarize_item(i) for i in record["items"][:200]],
    }
    return summary, None


def fetch_range(
    kind: str, from_str: str, to_str: str, workers: int, rate_limit: int
) -> tuple[dict | None, str | None]:
    if not REQUESTS_AVAILABLE:
        return None, "requests library not available"
    if kind not in ("sumario", "diario"):
        return None, f"invalid kind: {kind!r} (expected 'sumario' or 'diario')"
    try:
        dates = _date_range(from_str, to_str)
    except ValueError as e:
        return None, str(e)
    limiter = RateLimiter(rate_limit) if rate_limit else None
    workers = max(1, min(int(workers or DEFAULT_WORKERS), 16))
    days: list[dict] = []
    failed: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch_day, kind, d, limiter): d for d in dates}
        for fut in as_completed(futures):
            rec = fut.result()
            if rec["error"]:
                failed.append({"date": rec["date"], "error": rec["error"]})
            else:
                days.append({
                    "date": rec["date"],
                    "url": rec["url"],
                    "count": rec.get("count", 0),
                    "items": [_summarize_item(i) for i in rec["items"][:200]],
                })
    # Ordenar por fecha ascendente
    days.sort(key=lambda r: r["date"])
    return {
        "kind": kind,
        "from": dates[0],
        "to": dates[-1],
        "days_requested": len(dates),
        "days_succeeded": len(days),
        "days_failed": len(failed),
        "failed": failed,
        "days": days,
    }, None


def write_response(data: dict[str, Any]) -> None:
    print(json.dumps(data, default=str, ensure_ascii=False), flush=True)


def main() -> None:
    request: dict = {}
    try:
        request = json.loads(sys.stdin.read())
        request_id = request.get("request_id", "")
        args = request.get("arguments", {}) or {}

        kind = str(args.get("kind", DEFAULT_KIND)).strip().lower() or DEFAULT_KIND
        rate_limit = int(args.get("rate_limit", 0) or 0)
        if rate_limit <= 0:
            rate_limit = DEFAULT_RATE_LIMIT

        date = str(args.get("date", "")).strip()
        from_str = str(args.get("from", "")).strip()
        to_str = str(args.get("to", "")).strip()
        workers = int(args.get("workers", DEFAULT_WORKERS) or DEFAULT_WORKERS)

        if date and (from_str or to_str):
            write_response({
                "success": False, "request_id": request_id,
                "error": {"code": "INVALID_INPUT",
                          "message": "use either 'date' or 'from'+'to', not both"},
            })
            return

        if date:
            result, error = fetch_single(kind, date, rate_limit)
        elif from_str or to_str:
            if not (from_str and to_str):
                write_response({
                    "success": False, "request_id": request_id,
                    "error": {"code": "MISSING_RANGE",
                              "message": "'from' and 'to' are both required for a range"},
                })
                return
            result, error = fetch_range(kind, from_str, to_str, workers, rate_limit)
        else:
            # Por defecto: hoy
            today = datetime.now().strftime("%Y-%m-%d")
            result, error = fetch_single(kind, today, rate_limit)

        if error:
            write_response({
                "success": False, "request_id": request_id,
                "error": {"code": "FETCH_FAILED", "message": error},
            })
            return

        # A partir de aquí ``result`` no es None (lo garantiza el ``return`` previo).
        assert result is not None
        summary: dict = result

        # Texto humano
        if date or (not from_str and not to_str):
            lines = [
                f"**BOE — {summary['kind'].capitalize()} del {summary['date']}**",
                f"Disposiciones: {summary['count']}",
                f"URL: {summary['url']}",
            ]
        else:
            lines = [
                f"**BOE — {summary['kind'].capitalize()} {summary['from']} → {summary['to']}**",
                f"Días procesados: {summary['days_succeeded']}/{summary['days_requested']}"
                + (f" (fallos: {summary['days_failed']})" if summary['days_failed'] else ""),
            ]
            for day in summary["days"][:30]:  # cap en la vista humana
                lines.append(f"- {day['date']}: {day['count']} items")
            if len(summary["days"]) > 30:
                lines.append(f"- … ({len(summary['days']) - 30} días más en structured_content)")

        write_response({
            "success": True, "request_id": request_id,
            "content": [{"type": "text", "text": "\n".join(lines)}],
            "structured_content": {"source": "boe", **summary},
        })

    except json.JSONDecodeError:
        write_response({"success": False, "request_id": "",
                        "error": {"code": "INVALID_JSON", "message": "Failed to parse JSON"}})
    except Exception as e:
        logger.error("Unhandled exception", extra_data={"error": str(e)})
        write_response({
            "success": False, "request_id": request.get("request_id", ""),
            "error": {"code": "EXECUTION_FAILED", "message": str(e)},
        })


if __name__ == "__main__":
    main()
