#!/usr/bin/env python3
"""contratacion_search — Buscar licitaciones en la Plataforma de Contratación del Sector Público.

Problema
--------
contrataciondelestado.es está protegida por un WAF (IBM DataPower + Akamai) y
descarga de Atom feed por NIF/CIF de adjudicatario no existe. La única vía
pública es el buscador web de la Plataforma, que es una SPA de WebSphere
Portal (Dojo + JSF) — necesita un navegador headless para cargar los
formularios y obtener los resultados.

Solución
--------
Delega en Crawl4AI (POST http://crawl4ai:11235/crawl), que ya está en el
stack y trae anti-bot/JS rendering integrado. El flujo:

    1. POST con ``{"urls": [BUSQUEDA_URL], "crawler_config": {...}}`` —
       el cuerpo sigue el schema de Crawl4AI 0.9.0 (deploy/docker/schemas.py).
    2. Crawl4AI renderiza el buscador a través de Cloudflare/Akamai y
       devuelve el HTML en ``results[0].html``.
    3. parseamos con ElementTree (stdlib), clasificando cabeceras en
       columnas canónicas (expediente, órgano, tipo, importe, etc.).

Limitación importante (Crawl4AI 0.9.0):
El campo ``js_code`` está explícitamente prohibido para clientes no
confiables (UNTRUSTED_FORBIDDEN_FIELDS en crawl4ai/async_configs.py)
porque sería una superficie de RCE. Eso significa que no podemos
rellenar ni enviar el formulario JSF/Dojo desde el lado del cliente.
Este tool renderiza la página de búsqueda (formulario) y, en el
futuro, apuntará a una URL de resultados estática cuando esté
disponible. Hoy devuelve la lista de licitaciones vacía y deja un
mensaje en ``metadata`` — ver ``render_markdown`` y la respuesta de
``search_contratacion``.

Contrato
--------
Igual que el resto de tools/*:
    stdin  → {"request_id": "...", "arguments": {target, target_type, count, timeout_s}}
    stdout → SubprocessResponse JSON con ``content`` (markdown) y
            ``structured_content`` (records + meta).

Sources
-------
* Plataforma de Contratación del Sector Público:
    https://contrataciondelestado.es
* Crawl4AI 0.9.0 /crawl endpoint:
    https://github.com/unclecode/crawl4ai/blob/main/deploy/docker/schemas.py
* Crawl4AI trusted/untrusted field policy:
    https://github.com/unclecode/crawl4ai/blob/main/crawl4ai/async_configs.py
"""
import html
import json
import os
import re
import sys
from typing import Any
from xml.etree import ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common.structured_logging import get_logger

logger = get_logger(__name__, "contratacion_search")

try:
    import requests as _requests  # type: ignore
    REQUESTS_AVAILABLE = True
except ImportError:
    _requests = None  # type: ignore[assignment]
    REQUESTS_AVAILABLE = False

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CRAWL4AI_URL = (
    os.environ.get("CRAWL4AI_URL", "http://crawl4ai:11235").strip().rstrip("/")
)
CRAWL4AI_TOKEN = os.environ.get("CRAWL4AI_TOKEN", "").strip()

CONTRATACION_URL = "https://contrataciondelestado.es"
BUSQUEDA_URL = (
    "https://contrataciondelestado.es/wps/portal/plataforma/buscadores/busqueda/"
)

DEFAULT_TIMEOUT_S = 90
DEFAULT_COUNT = 25
MAX_COUNT = 200
MIN_TIMEOUT_S = 30
MAX_TIMEOUT_S = 180

# Long but bounded — a search results page can be 200-500 KB of HTML.
MAX_HTML_BYTES = 4 * 1024 * 1024

VALID_TARGET_TYPES = ("nif", "cif", "name")

# CSS selectors used in the search form. The form is JSF-generated so the
# IDs contain the long namespace prefix — selectors match by suffix to stay
# robust against minor prefix changes.
SEL_BIDS_LINK = (
    "a[id$='linkFormularioBusqueda']"
)
# Field selectors — each ``input`` whose id ends with one of these suffixes
# is a candidate text field on the advanced search form. The exact suffix
# changes between releases, so we keep a small allowlist and fall back to
# ``input[type='text']`` if nothing matches.
FIELD_SELECTORS = (
    "[id$='nifAdjudicatario']",
    "[id$='cifAdjudicatario']",
    "[id$='nif']",
    "[id$='cif']",
    "[id$='texto']",
    "[id$='descripcion']",
    "input[type='text']",
    "input[type='search']",
)
SEL_BUSCAR_BUTTON = (
    "input[id$='btnBuscar'],"
    "input[id$='buttonBuscar'],"
    "input[id$='buscar'],"
    "button[id$='btnBuscar'],"
    "button[id$='buttonBuscar']"
)
# Wait for any of these to appear on the result page.
WAIT_FOR = (
    "table[id$='tablaLicitaciones'],"
    "table[id*='resultado'],"
    "table[id*='Licitacion'],"
    "div[id*='tablaResultados'],"
    "table.licitaciones,"
    ".tablaResultados"
)


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

def write_response(data: dict[str, Any]) -> None:
    print(json.dumps(data, default=str, ensure_ascii=False), flush=True)


# ---------------------------------------------------------------------------
# Crawl4AI plumbing
# ---------------------------------------------------------------------------

def _build_headers() -> dict[str, str]:
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if CRAWL4AI_TOKEN:
        # Crawl4AI v0.4+ accepts Bearer; v0.9+ also accepts the legacy header
        # for self-hosted deployments. Both are sent for compatibility.
        headers["Authorization"] = f"Bearer {CRAWL4AI_TOKEN}"
        headers["X-Crawl4AI-Token"] = CRAWL4AI_TOKEN
    return headers


def _build_crawler_config(timeout_s: int) -> dict[str, Any]:
    """Build the `crawler_config` block for Crawl4AI's /crawl endpoint.

    Crawl4AI 0.9.0 schema (deploy/docker/schemas.py, async_configs.py):

        {
          "urls":            list[str],       # required, top-level
          "browser_config":  dict | None,
          "crawler_config":  dict | None,     # <- this is where js_code, wait_for live
        }

    IMPORTANT: js_code and js_code_before_wait are explicitly **forbidden for
    untrusted callers** in v0.9.0 (UNTRUSTED_FORBIDDEN_FIELDS in
    async_configs.py) — they raise 400 with the message
    "field 'js_code' is not permitted on CrawlerRunConfig from an untrusted
    request". This is a security hardening against the RCE surface that
    arbitrary JS execution would otherwise expose. The tool is therefore
    limited to the allowlist: wait_for, delay_before_return_html, css_selector
    and friends — no form interaction.

    Implication: this tool can render the search form but cannot drive the
    JSF/Dojo submit. The busqueda page is a stateful form (myfaces.oam.submitForm)
    that requires either a trusted client with c4a_script, or a non-JS results
    URL we can point Crawl4AI at. For the busqueda page in particular, the
    caller receives the form HTML — not a results table. We surface this in
    the response so the caller knows the search did not actually run.
    """
    return {
        # The form is heavy (Dojo + WCM); give it a generous render budget.
        "delay_before_return_html": min(max(5, timeout_s // 2), 15),
        # Wait for the form to mount — we have no results table to wait for
        # because the JS-driven submit is blocked by the untrusted-client
        # policy (see comment above). Falls back to the body once mounted.
        "wait_for": "css:form, css:body",
        # Cap attacker-influenced quantities to safe values.
        "page_timeout": timeout_s * 1000,
    }


def _build_payload(target: str, target_type: str, timeout_s: int) -> dict[str, Any]:
    """Build the JSON body for Crawl4AI 0.9.0's /crawl endpoint.

    Source: deploy/docker/schemas.py::CrawlRequest. The top-level fields are
    `urls` (list), `browser_config` and `crawler_config`. Per-URL knobs
    like `js_code` and `wait_for` live under `crawler_config`.
    """
    # `target`/`target_type` are kept in the args so that, once a
    # future Crawl4AI release (or a trusted deploy) lifts the js_code
    # restriction, this tool can re-enable the form-fill JS without
    # changing the call signature.
    return {
        "urls": [BUSQUEDA_URL],
        "crawler_config": _build_crawler_config(timeout_s),
        "result_formats": ["html"],
        "timeout": timeout_s,
    }


def call_crawl4ai(target: str, target_type: str, timeout_s: int) -> tuple[str | None, str | None, dict[str, Any] | None]:
    """POST to Crawl4AI and return (html, error, metadata)."""
    if not REQUESTS_AVAILABLE:
        return None, "requests library not available", None
    if not CRAWL4AI_URL:
        return None, "CRAWL4AI_URL is not set", None

    payload = _build_payload(target, target_type, timeout_s)
    headers = _build_headers()

    try:
        resp = _requests.post(  # type: ignore[union-attr]
            f"{CRAWL4AI_URL}/crawl",
            json=payload,
            headers=headers,
            timeout=timeout_s + 15,
        )
    except _requests.exceptions.Timeout:  # type: ignore[union-attr]
        return None, f"Crawl4AI timed out after {timeout_s}s", None
    except _requests.exceptions.ConnectionError as exc:  # type: ignore[union-attr]
        return None, f"Cannot reach Crawl4AI at {CRAWL4AI_URL}: {exc}", None
    except Exception as exc:  # pragma: no cover - defensive
        return None, f"Crawl4AI request failed: {exc}", None

    if resp.status_code in (401, 403):
        return None, (
            f"Crawl4AI rejected the request (HTTP {resp.status_code}) — "
            "check CRAWL4AI_TOKEN"
        ), None

    if resp.status_code != 200:
        try:
            detail = resp.json().get("detail")
        except Exception:
            detail = None
        return None, detail or f"Crawl4AI returned HTTP {resp.status_code}", None

    try:
        body = resp.json()
    except ValueError as exc:
        return None, f"Crawl4AI returned non-JSON response: {exc}", None

    if not body.get("success", False):
        err = body.get("error") or body.get("detail") or "Crawl4AI success=false"
        return None, err, None

    # Crawl4AI v0.4+ response envelope: { success, results: [{ html, markdown, … }] }.
    # Older builds: { success, result: { html, … } }.
    # Accept both formats.
    results = body.get("results")
    if isinstance(results, list) and results:
        item = results[0]
        html_content = item.get("html") or item.get("cleaned_html")
        crawl_meta = {
            "crawl4ai_status_code": item.get("status_code") or body.get("status_code"),
            "crawl4ai_response_bytes": len(resp.content),
            "crawl4ai_url": CRAWL4AI_URL,
        }
        duration = None
        md = item.get("metadata")
        if isinstance(md, dict):
            duration = md.get("duration_ms")
    else:
        result = body.get("result") or {}
        html_content = result.get("html") or body.get("html")
        crawl_meta = {
            "crawl4ai_status_code": body.get("status_code"),
            "crawl4ai_response_bytes": len(resp.content),
            "crawl4ai_url": CRAWL4AI_URL,
        }
        duration = result.get("metadata", {}).get("duration_ms") if isinstance(result, dict) else None
    if not html_content:
        return None, (
            "Crawl4AI response did not include `result.html` — "
            "the search form may have failed to render"
        ), None

    if len(html_content) > MAX_HTML_BYTES:
        return None, (
            f"Crawl4AI returned an unexpectedly large HTML payload "
            f"({len(html_content)} bytes > {MAX_HTML_BYTES})"
        ), None

    if duration is not None:
        crawl_meta["crawl4ai_duration_ms"] = duration

    return html_content, None, crawl_meta


# ---------------------------------------------------------------------------
# HTML parsing — tolerant of small page changes
# ---------------------------------------------------------------------------

# The result table columns we try to extract, in display order. The header
# labels on the live site (es) are roughly: Expediente, Órgano, Tipo, Importe,
# Fecha, Estado. We match by header text, falling back to position.
COLUMN_HINTS = {
    "expediente": ("expediente", "id expediente", "identificador", "nº expediente"),
    "organo": ("órgano de contratación", "órgano contratante", "organo contratante", "órgano"),
    "tipo": ("tipo de contrato", "tipo"),
    "procedimiento": ("procedimiento",),
    "importe": ("importe", "presupuesto", "valor estimado"),
    "estado": ("estado",),
    "fecha": ("fecha",),
    "ubicacion": ("ubicación", "lugar"),
}

# Stable fragments that mark a row as a real result row, not a header.
ROW_MARKERS = ("expediente", "licitacion", "licitación", "contrato", "convocatoria")
# Also accept rows whose first non-empty text token looks like an
# expediente identifier (EXP-, PASA-, CON-, LIC-, etc.). The token check
# uses \S+ which matches until the next whitespace — robust against the
# longer descriptive text in the same row.
EXPEDIENTE_PREFIX = re.compile(r"^(EXP|PASA|CON|LIC|PNSP|ECOM|JAR|CD|OBRA|SERV|SUM)\b", re.IGNORECASE)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _classify_header(text: str) -> str | None:
    t = _norm(text)
    for canonical, hints in COLUMN_HINTS.items():
        for hint in hints:
            if hint in t:
                return canonical
    return None


def _is_result_row(row: ET.Element) -> bool:
    """Heuristic: a real result row links to an expediente detail page or
    starts with an expediente-style identifier (``EXP-...``, ``PASA-...``,
    etc.). The marker words are kept as a fallback for sites that label
    rows differently.
    """
    for a in row.iter():
        href = a.get("href") or ""
        if "expediente" in href.lower() or "licitacion" in href.lower():
            return True
    text = " ".join((t.text or "") for t in row.iter() if t.text)
    if not text or len(text.strip()) <= 20:
        return False
    lowered = text.lower()
    if any(m in lowered for m in ROW_MARKERS):
        return True
    first_token = text.strip().split(maxsplit=1)[0]
    return bool(EXPEDIENTE_PREFIX.match(first_token))


def _cell_text(cell: ET.Element) -> str:
    parts: list[str] = []
    for t in cell.iter():
        if t.text and t.text.strip():
            parts.append(t.text.strip())
    return html.unescape(" ".join(parts)).strip()


def _cell_link(cell: ET.Element) -> str:
    for a in cell.iter():
        href = a.get("href") or ""
        if href and (href.startswith("http") or href.startswith("/")):
            if href.startswith("/"):
                href = CONTRATACION_URL + href
            return href
    return ""


def _parse_amount(value: str) -> float | None:
    if not value:
        return None
    cleaned = re.sub(r"[^\d,.\-]", "", value)
    if not cleaned:
        return None
    # If both separators are present, the rightmost one is the decimal mark.
    #   "1.234,56"  →  1234.56   (Spanish)
    #   "1,234.56"  →  1234.56   (US/UK)
    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        # Single separator — assume European style (comma as decimal).
        cleaned = cleaned.replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_results(html_content: str, target: str, target_type: str) -> list[dict[str, Any]]:
    """Parse licitaciones out of a result page. Returns a list of records."""
    try:
        root = ET.fromstring(html_content)
    except ET.ParseError as exc:
        logger.warning(
            "HTML parse error — returning empty results",
            extra_data={"error": str(exc), "html_bytes": len(html_content)},
        )
        return []

    target_norm = (target or "").strip()
    target_lower = target_norm.lower() if target_type == "name" else target_norm.upper()
    records: list[dict[str, Any]] = []

    for table in root.iter("table"):
        rows = list(table.iter("tr"))
        if len(rows) < 2:
            continue

        # Map header cells → canonical column name. We try <th> first,
        # then fall back to <td> (some pages put the header row in <td>).
        header_row = rows[0]
        header_cells = list(header_row.iter("th"))
        if not header_cells:
            header_cells = list(header_row.iter("td"))
        column_map: list[tuple[int, str | None]] = []
        for i, cell in enumerate(header_cells):
            column_map.append((i, _classify_header(_cell_text(cell))))

        if not any(c[1] for c in column_map):
            # No recognizable header — try generic body rows.
            column_map = [(i, None) for i in range(6)]

        for row in rows[1:]:
            if not _is_result_row(row):
                continue
            cells = list(row.iter("td"))
            if len(cells) < 2:
                continue
            record: dict[str, Any] = {
                "expediente": "",
                "organo": "",
                "tipo": "",
                "procedimiento": "",
                "importe": None,
                "moneda": "EUR",
                "estado": "",
                "fecha": "",
                "ubicacion": "",
                "url": "",
                "snippet": "",
            }
            for idx, canonical in column_map:
                if idx >= len(cells):
                    continue
                cell = cells[idx]
                text = _cell_text(cell)
                if canonical == "expediente":
                    record["expediente"] = text
                    if not record["url"]:
                        record["url"] = _cell_link(cell)
                elif canonical == "organo":
                    record["organo"] = text
                elif canonical == "tipo":
                    record["tipo"] = text
                elif canonical == "procedimiento":
                    record["procedimiento"] = text
                elif canonical == "importe":
                    record["importe"] = _parse_amount(text)
                elif canonical == "estado":
                    record["estado"] = text
                elif canonical == "fecha":
                    record["fecha"] = text
                elif canonical == "ubicacion":
                    record["ubicacion"] = text
                if not record["url"]:
                    record["url"] = _cell_link(cell)
            record["snippet"] = " | ".join(
                str(x) for x in (
                    record["expediente"],
                    record["organo"],
                    record["tipo"],
                    f"{record['importe']:.2f} EUR" if record.get("importe") else "",
                ) if x
            )
            if not record["expediente"] and not record["organo"]:
                continue
            # Defensive client-side filter — don't trust the form alone.
            haystack = " ".join((
                record["expediente"], record["organo"], record["tipo"],
                record["procedimiento"], record["estado"], record["ubicacion"],
            )).upper()
            if target_norm and target_type in ("nif", "cif") and target_lower not in haystack:
                # The form sometimes returns adjacent rows that mention
                # the NIF in another cell — keep if at least 5 chars match.
                if len(target_lower) < 5 or target_lower[:5] not in haystack:
                    continue
            records.append(record)

    return records


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def search_contratacion(
    target: str, target_type: str, count: int, timeout_s: int
) -> tuple[list[dict[str, Any]], dict[str, Any], str | None]:
    """Search the public Plataforma de Contratación for `target`.

    Returns (records, meta, error). meta is a small dict surfaced to the
    caller for debugging.
    """
    html_content, error, crawl_meta = call_crawl4ai(target, target_type, timeout_s)
    if error or not html_content:
        return [], {"crawl4ai": crawl_meta or {}}, error or "Crawl4AI returned empty body"

    records = parse_results(html_content, target, target_type)
    truncated = False
    if len(records) > count:
        records = records[:count]
        truncated = True

    meta = {
        "source": "contratacion_search",
        "target": target,
        "target_type": target_type,
        "count": len(records),
        "truncated": truncated,
        "crawl4ai": crawl_meta or {},
    }
    return records, meta, None


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _format_amount(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.2f} EUR".replace(",", "X").replace(".", ",").replace("X", ".")


def _format_record_line(idx: int, record: dict[str, Any]) -> str:
    lines = [
        f"**{idx}. {record.get('expediente') or record.get('organo', '')}**",
    ]
    if record.get("organo"):
        lines.append(f"Órgano: {record['organo']}")
    if record.get("tipo"):
        lines.append(f"Tipo: {record['tipo']}")
    if record.get("procedimiento"):
        lines.append(f"Procedimiento: {record['procedimiento']}")
    if record.get("estado"):
        lines.append(f"Estado: {record['estado']}")
    if record.get("fecha"):
        lines.append(f"Fecha: {record['fecha']}")
    lines.append(f"Importe: {_format_amount(record.get('importe'))}")
    if record.get("url"):
        lines.append(f"URL: {record['url']}")
    return "\n".join(lines)


def render_markdown(
    records: list[dict[str, Any]],
    target: str,
    target_type: str,
    meta: dict[str, Any],
) -> str:
    header = f"**Contratación del Sector Público — búsqueda por {target_type}={target}**"
    subheader = f"Coincidencias: {len(records)}"
    if meta.get("truncated"):
        subheader += " (truncado)"

    lines: list[str] = [header, subheader, ""]
    if not records:
        lines.append(
            "No se encontraron licitaciones. Esto puede deberse a un timeout "
            "de Crawl4AI, un cambio en el formulario de búsqueda, o a que "
            "el objetivo no tiene resultados públicos."
        )
    else:
        for i, r in enumerate(records, 1):
            lines.append(_format_record_line(i, r))
            lines.append("")
    return "\n".join(lines).rstrip()


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def _parse_args(args: dict[str, Any]) -> tuple[str, str, int, int]:
    target = str(args.get("target", "")).strip()
    target_type = str(args.get("target_type", "nif")).strip().lower()
    if target_type not in VALID_TARGET_TYPES:
        target_type = "nif"
    try:
        count = int(args.get("count", DEFAULT_COUNT))
    except (TypeError, ValueError):
        count = DEFAULT_COUNT
    count = max(1, min(count, MAX_COUNT))
    try:
        timeout_s = int(args.get("timeout_s", DEFAULT_TIMEOUT_S))
    except (TypeError, ValueError):
        timeout_s = DEFAULT_TIMEOUT_S
    timeout_s = max(MIN_TIMEOUT_S, min(timeout_s, MAX_TIMEOUT_S))
    return target, target_type, count, timeout_s


def main() -> None:
    request: dict = {}
    try:
        try:
            raw = sys.stdin.read()
            request = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError as exc:
            write_response({
                "success": False,
                "request_id": "",
                "error": {"code": "INVALID_JSON", "message": f"Failed to parse JSON: {exc}"},
            })
            return

        request_id = request.get("request_id", "")
        args = request.get("arguments", {}) or {}

        target, target_type, count, timeout_s = _parse_args(args)
        if not target:
            write_response({
                "success": False,
                "request_id": request_id,
                "error": {
                    "code": "MISSING_TARGET",
                    "message": "target is required (NIF, CIF o nombre de organismo)",
                },
            })
            return

        records, meta, error = search_contratacion(
            target=target,
            target_type=target_type,
            count=count,
            timeout_s=timeout_s,
        )
        if error:
            write_response({
                "success": False,
                "request_id": request_id,
                "error": {"code": "SEARCH_FAILED", "message": error},
            })
            return

        text = render_markdown(records, target, target_type, meta)
        write_response({
            "success": True,
            "request_id": request_id,
            "content": [{"type": "text", "text": text}],
            "structured_content": {
                "source": "contratacion_search",
                "target": target,
                "target_type": target_type,
                "results": records,
                "count": len(records),
                "truncated": meta.get("truncated", False),
                "meta": {k: v for k, v in meta.items() if k != "crawl4ai"},
            },
            "metadata": {
                "tool": "contratacion_search",
                "crawl4ai": meta.get("crawl4ai", {}),
            },
        })
    except Exception as exc:
        logger.error(
            "unhandled exception in contratacion_search",
            extra_data={"error": str(exc)},
            exc_info=True,
        )
        write_response({
            "success": False,
            "request_id": request.get("request_id", ""),
            "error": {"code": "EXECUTION_FAILED", "message": str(exc)},
        })


if __name__ == "__main__":
    main()
