#!/usr/bin/env python3
"""borme_fetch — Descarga sumarios del BORME (Boletín Oficial del Registro Mercantil).

Lee una petición JSON por STDIN con los campos definidos en ``tool.yaml`` y
devuelve un ``SubprocessResponse`` (success/content/structured_content) por
STDOUT. Sigue el mismo patrón que ``tools/ted_search/main.py``,
``tools/boe_search/main.py`` y ``tools/boe/main.py``.

Modos de uso (uno u otro):
  - ``date``  → una sola fecha (YYYY-MM-DD)
  - ``from`` + ``to`` → rango histórico de fechas (YYYY-MM-DD / YYYY-MM-DD)

Campos opcionales:
  - ``workers``    → nº de workers concurrentes para el rango (default 4)
  - ``rate_limit`` → requests por minuto (rate limit básico client-side)

API: https://www.boe.es/datosabiertos/api/borme/sumario/{YYYYMMDD}
La API de BORME sólo expone el sumario diario (no hay endpoint "diario"
como en BOE), por lo que este script no incluye un campo ``kind``.

Estructura de la respuesta (verificada 2025-07):
  {
    "status": {"code": "200", "text": "ok"},
    "data": {
      "sumario": {
        "metadatos": {...},
        "diario": [
          {
            "numero": 132,
            "sumario_diario": {...},
            "seccion": [
              {
                "codigo": "A",
                "nombre": "SECCIÓN PRIMERA. Empresarios. Actos inscritos",
                "item": [
                  {
                    "identificador": "BORME-A-2025-132-03",
                    "titulo": "ALICANTE/ALACANT",
                    "url_pdf":  {"texto": "...", "pagina_inicial": ..., ...},
                    "url_html": "https://www.boe.es/diario_borme/txt.php?id=...",
                    "url_xml":  "https://www.boe.es/diario_borme/xml.php?id=..."
                  },
                  ...
                ]
              },
              ...
            ]
          }
        ]
      }
    }
  }

Las secciones C en adelante (Actos de inscripción registral, etc.) a veces
no tienen ``item``; se ignoran sin error.
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

logger = get_logger(__name__, "borme_fetch")

from common.http import request_with_retry, REQUESTS_AVAILABLE

BORME_API_BASE = "https://www.boe.es/datosabiertos/api"
DEFAULT_WORKERS = 4
DEFAULT_RATE_LIMIT = 30  # req/min — conservador; BOE no documenta límite duro
MAX_RANGE_DAYS = 365  # tope para evitar rangos absurdos
MAX_ITEMS_PER_DAY = 200  # tope por día para no inflar el payload


def _to_borme_date(s: str) -> str:
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


def _extract_items(data: dict) -> list[dict]:
    """Recorre data.data.sumario.diario[*].seccion[*].item y devuelve la lista plana.

    La API de BORME estructura los actos en secciones (A, B, C, …). Aplanamos
    la jerarquía a una lista de items anotada con la sección de origen.

    La respuesta tiene la forma ``{status, data: {sumario: {metadatos, diario}}}``,
    por lo que hay que bajar dos niveles antes de llegar al sumario.
    """
    items: list[dict] = []
    try:
        # BORME envuelve el sumario en data.data.sumario (a diferencia del BOE
        # que lo expone directamente en data.sumario).
        inner = (data or {}).get("data") or {}
        sumario = inner.get("sumario") or {}
        for diario in sumario.get("diario", []) or []:
            for seccion in diario.get("seccion", []) or []:
                sec_codigo = seccion.get("codigo", "")
                sec_nombre = seccion.get("nombre", "")
                raw_items = seccion.get("item")
                if raw_items is None:
                    continue
                if isinstance(raw_items, dict):
                    raw_items = [raw_items]
                for item in raw_items:
                    if isinstance(item, dict):
                        enriched = dict(item)
                        enriched.setdefault("_seccion_codigo", sec_codigo)
                        enriched.setdefault("_seccion_nombre", sec_nombre)
                        items.append(enriched)
    except Exception as e:  # noqa: BLE001
        logger.warning("Failed to extract BORME items", extra_data={"error": str(e)})
    return items


def fetch_day(borme_date: str, rate_limiter: "RateLimiter | None" = None) -> dict:
    """Descarga el sumario de un día. Devuelve dict con metadatos y ``items``."""
    url = f"{BORME_API_BASE}/borme/sumario/{borme_date}"
    record: dict[str, Any] = {
        "date": borme_date,
        "url": url,
        "status": None,
        "error": None,
        "items": [],
    }
    if not REQUESTS_AVAILABLE:
        record["error"] = "requests library not available"
        return record
    if rate_limiter is not None:
        rate_limiter.wait()
    try:
        resp = request_with_retry("GET", url, headers={"Accept": "application/json"}, timeout=20)
        record["status"] = resp.status_code
        if resp.status_code != 200:
            record["error"] = f"HTTP {resp.status_code}"
            return record
        data = resp.json()
        # La API de BORME envuelve el sumario en data.sumario.diario[…].seccion[…].item
        record["items"] = _extract_items(data)
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


def _summarize_item(item: Any) -> dict:
    """Extrae campos representativos de un item del BORME para evitar payloads enormes.

    Campos del BORME: identificador, titulo, url_pdf (dict con 'texto' y metadatos),
    url_html, url_xml. A veces aparecen campos legacy ``url`` en texto plano.
    """
    if not isinstance(item, dict):
        return {"raw": str(item)[:300]}
    out: dict[str, Any] = {
        "identificador": item.get("identificador") or item.get("id"),
        "titulo": item.get("titulo") or item.get("title"),
        "url_pdf": (item.get("url_pdf") or {}).get("texto") if isinstance(item.get("url_pdf"), dict) else item.get("url_pdf"),
        "url_html": item.get("url_html"),
        "url_xml": item.get("url_xml"),
        "seccion_codigo": item.get("_seccion_codigo"),
        "seccion_nombre": item.get("_seccion_nombre"),
    }
    # Mantenemos sólo campos con valor
    return {k: v for k, v in out.items() if v not in (None, "")}


def fetch_single(date_str: str, rate_limit: int) -> tuple[dict | None, str | None]:
    if not REQUESTS_AVAILABLE:
        return None, "requests library not available"
    try:
        borme_date = _to_borme_date(date_str)
    except ValueError:
        return None, f"invalid date format: {date_str!r} (expected YYYY-MM-DD)"
    limiter = RateLimiter(rate_limit) if rate_limit else None
    record = fetch_day(borme_date, limiter)
    if record["error"]:
        return None, f"BORME fetch failed for {borme_date}: {record['error']}"
    items = record["items"][:MAX_ITEMS_PER_DAY]
    return {
        "date": borme_date,
        "url": record["url"],
        "count": record.get("count", 0),
        "items": [_summarize_item(i) for i in items],
        "truncated": record.get("count", 0) > len(items),
    }, None


def fetch_range(
    from_str: str, to_str: str, workers: int, rate_limit: int
) -> tuple[dict | None, str | None]:
    if not REQUESTS_AVAILABLE:
        return None, "requests library not available"
    try:
        dates = _date_range(from_str, to_str)
    except ValueError as e:
        return None, str(e)
    limiter = RateLimiter(rate_limit) if rate_limit else None
    workers = max(1, min(int(workers or DEFAULT_WORKERS), 16))
    days: list[dict] = []
    failed: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch_day, d, limiter): d for d in dates}
        for fut in as_completed(futures):
            rec = fut.result()
            if rec["error"]:
                failed.append({"date": rec["date"], "error": rec["error"]})
            else:
                items = rec["items"][:MAX_ITEMS_PER_DAY]
                days.append({
                    "date": rec["date"],
                    "url": rec["url"],
                    "count": rec.get("count", 0),
                    "truncated": rec.get("count", 0) > len(items),
                    "items": [_summarize_item(i) for i in items],
                })
    # Ordenar por fecha ascendente
    days.sort(key=lambda r: r["date"])
    return {
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
            result, error = fetch_single(date, rate_limit)
        elif from_str or to_str:
            if not (from_str and to_str):
                write_response({
                    "success": False, "request_id": request_id,
                    "error": {"code": "MISSING_RANGE",
                              "message": "'from' and 'to' are both required for a range"},
                })
                return
            result, error = fetch_range(from_str, to_str, workers, rate_limit)
        else:
            # Por defecto: hoy
            today = datetime.now().strftime("%Y-%m-%d")
            result, error = fetch_single(today, rate_limit)

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
                f"**BORME — Sumario del {summary['date']}**",
                f"Actos: {summary['count']}"
                + (" (truncado a 200)" if summary.get("truncated") else ""),
                f"URL: {summary['url']}",
            ]
        else:
            lines = [
                f"**BORME — Sumario {summary['from']} → {summary['to']}**",
                f"Días procesados: {summary['days_succeeded']}/{summary['days_requested']}"
                + (f" (fallos: {summary['days_failed']})" if summary['days_failed'] else ""),
            ]
            for day in summary["days"][:30]:  # cap en la vista humana
                truncated_note = " (truncado)" if day.get("truncated") else ""
                lines.append(f"- {day['date']}: {day['count']} actos{truncated_note}")
            if len(summary["days"]) > 30:
                lines.append(f"- … ({len(summary['days']) - 30} días más en structured_content)")
            if summary["failed"]:
                lines.append("")
                lines.append("**Fallos:**")
                for f in summary["failed"][:10]:
                    lines.append(f"- {f['date']}: {f['error']}")

        write_response({
            "success": True, "request_id": request_id,
            "content": [{"type": "text", "text": "\n".join(lines)}],
            "structured_content": {"source": "borme", **summary},
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
