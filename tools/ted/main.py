#!/usr/bin/env python3
"""ted_fetch — Descarga histórica en batch de licitaciones europeas desde TED.

Lee una petición JSON por STDIN con los campos definidos en ``tool.yaml`` y
devuelve un ``SubprocessResponse`` (success/content/structured_content) por
STDOUT. Sigue el mismo patrón que ``tools/boe/main.py`` y
``tools/ted_search/main.py``.

Modos de uso (uno u otro):
  - ``date``  → una sola fecha (YYYY-MM-DD)
  - ``from`` + ``to`` → rango histórico de fechas (YYYY-MM-DD / YYYY-MM-DD)

Campos opcionales:
  - ``rows``       → máximo de notices a devolver (1-1000, default 100)
  - ``scope``      → "active" (default) o "all" (incluye canceladas)
  - ``workers``    → nº de workers concurrentes para paginación (default 4, max 8)

Diferencia con ``ted_search``:
  - ``ted_search`` busca por NIF/nombre de organización (consulta selectiva)
  - ``ted_fetch``  descarga el flujo histórico completo por fecha (batch)

API: TED v3 Search (https://api.ted.europa.eu/v3/notices/search)
Paginación: ``page`` 1-based + ``limit`` (max 100 por página).
"""

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common.structured_logging import get_logger  # noqa: E402

logger = get_logger(__name__, "ted_fetch")

try:
    import requests as _requests  # noqa: F401
    requests = _requests
    REQUESTS_AVAILABLE = True
except ImportError:
    requests = None  # type: ignore[assignment]
    REQUESTS_AVAILABLE = False

TED_SEARCH_URL = "https://api.ted.europa.eu/v3/notices/search"
DEFAULT_ROWS = 100
DEFAULT_WORKERS = 4
MAX_WORKERS = 8
PAGE_SIZE = 100  # API max — fixed, no se negocia
MAX_ROWS = 1000
MAX_RANGE_DAYS = 31  # tope para evitar rangos absurdos en fetch diario
DEFAULT_SCOPE = "active"

# Campos por defecto que devolvemos del API. Si el caller quiere más, podría
# añadirse en un futuro; por ahora cubrimos el set de metadatos OSINT típicos.
DEFAULT_FIELDS = [
    "ND",
    "publication-date",
    "publication-number",
    "notice-title",
    "buyer-name",
    "buyer-country",
    "organisation-identifier-buyer",
    "classification-cpv",
    "notice-type",
    "deadline-date-lot",
    "procedure-type",
]


def _to_ted_date(s: str) -> str:
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


def _build_query(from_ted: str, to_ted: str) -> str:
    """Construye la query expert de TED: rango de publication-date."""
    return f"PD>={from_ted} AND PD<={to_ted}"


def _flatten_buyer(n: dict) -> tuple[str, str]:
    """TED devuelve buyer-name como dict {lang: [name,...]} y buyer-country
    como lista de códigos ISO. Extraemos el nombre en español si está, si no
    el primero disponible, y el primer código de país."""
    name_obj = n.get("buyer-name") or {}
    country_obj = n.get("buyer-country") or []

    name = ""
    if isinstance(name_obj, dict):
        # Preferir español, luego inglés, luego cualquier idioma
        for lang in ("spa", "eng"):
            vals = name_obj.get(lang)
            if vals:
                name = vals[0] if isinstance(vals, list) else str(vals)
                break
        if not name:
            for vals in name_obj.values():
                if vals:
                    name = vals[0] if isinstance(vals, list) else str(vals)
                    break

    country = ""
    if isinstance(country_obj, list):
        if country_obj:
            country = str(country_obj[0])
    elif isinstance(country_obj, str):
        country = country_obj

    return name, country


def _flatten_cpv(n: dict) -> list[str]:
    """classification-cpv viene como lista de strings (códigos CPV)."""
    cpc = n.get("classification-cpv") or []
    if isinstance(cpc, list):
        return [str(x) for x in cpc]
    if isinstance(cpc, str):
        return [cpc]
    return []


def _extract_first_deadline(n: dict) -> str:
    """El deadline a veces está en deadline-date-lot top-level, a veces
    dentro de un array lots[].date. Buscamos ambos."""
    dl = n.get("deadline-date-lot") or ""
    if dl:
        return str(dl)[:10]
    lots = n.get("lots") or []
    if isinstance(lots, list) and lots:
        first = lots[0]
        if isinstance(first, dict):
            dl2 = first.get("deadline-date-lot") or first.get("deadline-receipt-tender") or ""
            if dl2:
                return str(dl2)[:10]
    return ""


def _summarize_notice(n: dict) -> dict:
    """Reduce un notice TED a un set de campos manejables para OSINT."""
    if not isinstance(n, dict):
        return {"raw": str(n)[:300]}
    nd = n.get("ND") or n.get("notice-id") or ""
    pub_date = (n.get("publication-date") or "")[:10]  # cortar ISO timezone
    title_obj = n.get("notice-title") or {}
    if isinstance(title_obj, dict):
        title = title_obj.get("spa") or title_obj.get("eng") or ""
        if not title:
            for v in title_obj.values():
                if v:
                    title = v if isinstance(v, str) else (v[0] if v else "")
                    break
    else:
        title = str(title_obj)
    buyer_name, buyer_country = _flatten_buyer(n)
    out: dict[str, Any] = {
        "notice_id": nd,
        "publication_date": pub_date,
        "title": title or "",
        "buyer_name": buyer_name,
        "buyer_country": buyer_country,
        "buyer_national_id": n.get("organisation-identifier-buyer") or "",
        "cpv_codes": _flatten_cpv(n),
        "notice_type": n.get("notice-type") or "",
        "procedure_type": n.get("procedure-type") or "",
        "deadline": _extract_first_deadline(n),
    }
    return {k: v for k, v in out.items() if v not in (None, "", [])}


def _fetch_page(page: int, from_ted: str, to_ted: str, scope: str) -> dict:
    """Descarga una página de resultados. Devuelve dict con metadatos."""
    record: dict[str, Any] = {
        "page": page,
        "status": None,
        "error": None,
        "notices": [],
        "total": 0,
    }
    if not REQUESTS_AVAILABLE:
        record["error"] = "requests library not available"
        return record
    payload = {
        "query": _build_query(from_ted, to_ted),
        "fields": DEFAULT_FIELDS,
        "limit": PAGE_SIZE,
        "page": page,
        "scope": scope.upper(),
    }
    try:
        assert requests is not None
        resp = requests.post(
            TED_SEARCH_URL,
            json=payload,
            headers={"Accept": "application/json"},
            timeout=30,
        )
        record["status"] = resp.status_code
        if resp.status_code != 200:
            record["error"] = f"HTTP {resp.status_code}"
            return record
        data = resp.json()
        record["notices"] = data.get("notices") or []
        record["total"] = data.get("totalNoticeCount") or 0
        record["timed_out"] = bool(data.get("timedOut", False))
    except requests.exceptions.Timeout:  # type: ignore[union-attr]
        record["error"] = "timeout"
    except requests.exceptions.RequestException as e:  # type: ignore[union-attr]
        record["error"] = f"request error: {e}"
    except json.JSONDecodeError:
        record["error"] = "invalid json response"
    except Exception as e:  # noqa: BLE001
        record["error"] = f"unexpected: {e}"
    return record


def _paginate(from_ted: str, to_ted: str, rows: int, scope: str, workers: int) -> tuple[dict | None, str | None]:
    """Pagina hasta ``rows`` notices o hasta agotar el total del API."""
    # Primera llamada: cuántas páginas necesitamos?
    first = _fetch_page(1, from_ted, to_ted, scope)
    if first["error"]:
        return None, f"TED fetch failed on page 1: {first['error']}"
    total = first["total"]
    if total == 0:
        return {
            "from": from_ted,
            "to": to_ted,
            "scope": scope,
            "total_available": 0,
            "rows_requested": rows,
            "rows_returned": 0,
            "pages_fetched": 1,
            "pages_failed": 0,
            "failed": [],
            "notices": [],
            "timed_out": first.get("timed_out", False),
        }, None
    pages_needed = min((rows + PAGE_SIZE - 1) // PAGE_SIZE, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    notices: list[dict] = list(first["notices"])
    failed: list[dict] = []
    if pages_needed > 1:
        # Páginas 2..pages_needed en paralelo
        page_nums = list(range(2, pages_needed + 1))
        with ThreadPoolExecutor(max_workers=min(workers, MAX_WORKERS)) as pool:
            futures = {
                pool.submit(_fetch_page, p, from_ted, to_ted, scope): p for p in page_nums
            }
            for fut in as_completed(futures):
                rec = fut.result()
                if rec["error"]:
                    failed.append({"page": rec["page"], "error": rec["error"]})
                else:
                    notices.extend(rec["notices"])
                    if rec.get("timed_out"):
                        # Marcar pero no fallar — devolvemos lo que tengamos
                        failed.append({"page": rec["page"], "error": "partial (timedOut)"})
    # Cortar a ``rows`` y resumir
    notices = notices[:rows]
    summarized = [_summarize_notice(n) for n in notices]
    return {
        "from": from_ted,
        "to": to_ted,
        "scope": scope,
        "total_available": total,
        "rows_requested": rows,
        "rows_returned": len(summarized),
        "pages_fetched": pages_needed,
        "pages_failed": len(failed),
        "failed": failed,
        "notices": summarized,
        "timed_out": first.get("timed_out", False),
    }, None


def _fetch_single(date_iso: str, rows: int, scope: str, workers: int) -> tuple[dict | None, str | None]:
    if not REQUESTS_AVAILABLE:
        return None, "requests library not available"
    try:
        ted_date = _to_ted_date(date_iso)
    except ValueError:
        return None, f"invalid date format: {date_iso!r} (expected YYYY-MM-DD)"
    return _paginate(ted_date, ted_date, rows, scope, workers)


def _fetch_range(from_str: str, to_str: str, rows: int, scope: str, workers: int) -> tuple[dict | None, str | None]:
    if not REQUESTS_AVAILABLE:
        return None, "requests library not available"
    try:
        dates = _date_range(from_str, to_str)
    except ValueError as e:
        return None, str(e)
    # Concatenar fechas con OR en la query — una sola llamada API, sin re-paginar
    # múltiples veces. Si la primera fecha del rango no tiene resultados, la
    # query sigue siendo válida (el API devuelve totalNoticeCount=0).
    if len(dates) == 1:
        return _paginate(dates[0], dates[0], rows, scope, workers)
    # Para múltiples fechas: una query por fecha en paralelo, luego agregamos
    from_ted = dates[0]
    to_ted = dates[-1]
    # Usamos el rango completo: PD>=from AND PD<=to — el API entiende rangos
    return _paginate(from_ted, to_ted, rows, scope, workers)


def write_response(data: dict[str, Any]) -> None:
    print(json.dumps(data, default=str, ensure_ascii=False), flush=True)


def _format_human(summary: dict, single: bool) -> str:
    lines: list[str] = []
    scope_label = "activas" if summary["scope"] == "active" else "totales (incluye canceladas)"
    if single:
        lines.append(f"**TED — Licitaciones UE {scope_label} del {summary['from']}**")
    else:
        lines.append(
            f"**TED — Licitaciones UE {scope_label} {summary['from']} → {summary['to']}**"
        )
    lines.append(
        f"Notices devueltos: {summary['rows_returned']} / {summary['rows_requested']} solicitados "
        f"(disponibles en el API: {summary['total_available']:,})"
        .replace(",", ".")
    )
    lines.append(
        f"Páginas: {summary['pages_fetched']} procesadas, {summary['pages_failed']} con error"
    )
    if summary.get("timed_out"):
        lines.append("⚠️ El API marcó ``timedOut=true`` — los resultados son parciales.")
    if summary["rows_returned"] == 0:
        lines.append("No se encontraron notices en el rango solicitado.")
        return "\n".join(lines)
    # Mostrar las 10 primeras
    shown = summary["notices"][:10]
    for i, n in enumerate(shown, 1):
        buyer = n.get("buyer_name", "")
        country = n.get("buyer_country", "")
        cpv = ", ".join(n.get("cpv_codes", [])[:3])
        lines.append(
            f"**{i}. {n.get('title', '(sin título)')}**\n"
            f"   ID: {n.get('notice_id', '')} | Pub: {n.get('publication_date', '')}\n"
            f"   Comprador: {buyer} ({country})"
        )
        if cpv:
            lines.append(f"   CPV: {cpv}")
    if summary["rows_returned"] > 10:
        lines.append(f"… ({summary['rows_returned'] - 10} notices más en structured_content)")
    return "\n".join(lines)


def main() -> None:
    request: dict = {}
    try:
        request = json.loads(sys.stdin.read())
        request_id = request.get("request_id", "")
        args = request.get("arguments", {}) or {}

        date = str(args.get("date", "")).strip()
        from_str = str(args.get("from", "")).strip()
        to_str = str(args.get("to", "")).strip()
        rows = int(args.get("rows", DEFAULT_ROWS) or DEFAULT_ROWS)
        rows = max(1, min(rows, MAX_ROWS))
        scope = str(args.get("scope", DEFAULT_SCOPE)).strip().lower() or DEFAULT_SCOPE
        if scope not in ("active", "all"):
            scope = DEFAULT_SCOPE
        workers = int(args.get("workers", DEFAULT_WORKERS) or DEFAULT_WORKERS)
        workers = max(1, min(workers, MAX_WORKERS))

        if date and (from_str or to_str):
            write_response({
                "success": False, "request_id": request_id,
                "error": {"code": "INVALID_INPUT",
                          "message": "use either 'date' or 'from'+'to', not both"},
            })
            return

        if date:
            result, error = _fetch_single(date, rows, scope, workers)
            single = True
        elif from_str or to_str:
            if not (from_str and to_str):
                write_response({
                    "success": False, "request_id": request_id,
                    "error": {"code": "MISSING_RANGE",
                              "message": "'from' and 'to' are both required for a range"},
                })
                return
            result, error = _fetch_range(from_str, to_str, rows, scope, workers)
            single = False
        else:
            # Por defecto: hoy
            today = datetime.now().strftime("%Y-%m-%d")
            result, error = _fetch_single(today, rows, scope, workers)
            single = True

        if error:
            write_response({
                "success": False, "request_id": request_id,
                "error": {"code": "FETCH_FAILED", "message": error},
            })
            return

        assert result is not None
        human = _format_human(result, single)
        write_response({
            "success": True, "request_id": request_id,
            "content": [{"type": "text", "text": human}],
            "structured_content": {"source": "ted", **result},
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
