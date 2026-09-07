#!/usr/bin/env python3
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common.structured_logging import get_logger
from common.http import request_with_retry, REQUESTS_AVAILABLE
from common.evidence import build_evidence, build_search_result
from common.entity_normalizer import normalize_for_match

logger = get_logger(__name__, "borme_search")

BORME_API = "https://www.boe.es/datosabiertos/api/borme/sumario"
BORME_MAX_RANGE_DAYS = 90
DEFAULT_COUNT = 20


def _default_lookback_days() -> int:
    """Return BORME_LOOKBACK_DAYS (default 7), clamped to >= 1."""
    try:
        return max(1, int(os.environ.get("BORME_LOOKBACK_DAYS", "7")))
    except (TypeError, ValueError):
        return 7


def _as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def iter_borme_items(data: dict) -> list[dict]:
    """Flatten the BORME sumario into enriched item dicts.

    Section A items sit at seccion.item[] (titulo = province); section C
    items sit at seccion.apartado[].item[] (titulo = entity).
    Each item carries 'seccion' ('A'|'C') and 'apartado' (name or '').
    """
    out: list[dict] = []
    try:
        diario = data["data"]["sumario"]["diario"]
    except (KeyError, TypeError):
        return out
    for day in _as_list(diario):
        for seccion in _as_list(day.get("seccion")):
            sec_code = seccion.get("codigo", "")
            for it in _as_list(seccion.get("item")):
                if isinstance(it, dict):
                    enriched = dict(it)
                    enriched["seccion"] = sec_code
                    enriched["apartado"] = ""
                    out.append(enriched)
            for ap in _as_list(seccion.get("apartado")):
                for it in _as_list(ap.get("item")):
                    if isinstance(it, dict):
                        enriched = dict(it)
                        enriched["seccion"] = sec_code
                        enriched["apartado"] = ap.get("nombre", "")
                        out.append(enriched)
    return out


def name_matches(target: str, candidate: str) -> bool:
    """Bidirectional normalized substring match for company names."""
    tn = normalize_for_match(target)
    cn = normalize_for_match(candidate)
    return bool(tn and cn and (tn in cn or cn in tn))


_COMPANY_PREFIX_RE = re.compile(r'^\s*\d+\s*[-–]\s*')


def _clean_company_name(raw: str) -> str:
    text = (raw or "").strip()
    text = _COMPANY_PREFIX_RE.sub("", text)
    return text.rstrip(".").strip()


def parse_province_xml(xml_text: str) -> list[dict]:
    """Parse a BORME province XML into [{'name', 'acts'}, ...].

    Company headings are <p class="articulo"> ("NNN - NAME.") followed by
    one or more <p class="parrafo"> with the registered acts.
    """
    companies: list[dict] = []
    if not xml_text:
        return companies
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.warning(
            "BORME province XML parse failed",
            extra_data={"error": str(exc)},
        )
        return companies
    current: dict | None = None
    for p in root.iter("p"):
        cls = (p.get("class") or "").strip()
        text = "".join(p.itertext()).strip()
        if not text:
            continue
        if cls == "articulo":
            current = {"name": _clean_company_name(text), "acts": []}
            companies.append(current)
        elif cls == "parrafo" and current is not None:
            current["acts"].append(text)
    return companies


def fetch_province_xml(url_xml: str) -> str | None:
    """GET a BORME province XML. Returns text, or None on failure."""
    if not url_xml:
        return None
    try:
        resp = request_with_retry(
            "GET",
            url_xml,
            headers={"Accept": "application/xml, text/xml, */*"},
            timeout=15,
            max_retries=2,
        )
    except Exception as exc:
        logger.warning(
            "BORME province fetch failed",
            extra_data={"url": url_xml, "error": str(exc)},
        )
        return None
    if resp.status_code != 200:
        logger.warning(
            "BORME province HTTP error",
            extra_data={"url": url_xml, "status_code": resp.status_code},
        )
        return None
    return resp.text


def date_range(from_str: str, to_str: str) -> list[str]:
    fmt = "%Y-%m-%d"
    start = datetime.strptime(from_str, fmt) if from_str else datetime.now() - timedelta(days=_default_lookback_days())
    end = datetime.strptime(to_str, fmt) if to_str else datetime.now()
    dates = []
    current = start
    while current <= end:
        dates.append(current.strftime("%Y%m%d"))
        current += timedelta(days=1)
    return dates


def fetch_borme_day(date_str: str) -> list[dict]:
    url = f"{BORME_API}/{date_str}"
    try:
        resp = request_with_retry(
            "GET", url, headers={"Accept": "application/json"}, timeout=15
        )
    except Exception as exc:
        logger.warning(
            "BORME request failed",
            extra_data={"date": date_str, "error": str(exc)},
        )
        return []
    if resp.status_code != 200:
        return []
    try:
        return iter_borme_items(resp.json())
    except Exception as exc:
        logger.warning(
            "BORME parse failed",
            extra_data={"date": date_str, "error": str(exc)},
        )
        return []


def _evidence_from_secondary_item(item: dict, target: str, day_iso: str) -> list[dict] | None:
    """Section C: match on the sumario item title, no XML download."""
    title = str(item.get("titulo", ""))
    if not name_matches(target, title):
        return None
    return [build_evidence(
        source="borme",
        official=True,
        confidence=0.9,
        title=title,
        date=day_iso,
        url=str(item.get("url_html", "") or ""),
        metadata={
            "identificador": item.get("identificador", ""),
            "seccion": item.get("seccion", "C"),
            "apartado": item.get("apartado", ""),
        },
        entity=target,
        query=target,
    )]


# Accepted cost model (human-reviewed): full per-day sweep. Every section A
# province XML for the submitted day is fetched before matching completes —
# all fetches are submitted up front and the ThreadPoolExecutor drains any
# pending ones on exit. Latency is bounded by the slowest province of the
# day; results are deterministic for a given sumario. Do NOT restructure
# (e.g. into a lazy/cancellable fetch) without revisiting this decision.
def _search_primary_day(
    items: list[dict], target: str, count: int, day_iso: str
) -> list[dict]:
    """Section A: fetch each province XML concurrently and match company names."""
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = [ex.submit(fetch_province_xml, it.get("url_xml", "")) for it in items]
        for it, fut in zip(items, futures):
            xml_text = fut.result()
            if not xml_text:
                continue
            for comp in parse_province_xml(xml_text):
                if not name_matches(target, comp["name"]):
                    continue
                results.append(build_evidence(
                    source="borme",
                    official=True,
                    confidence=0.9,
                    title=comp["name"],
                    date=day_iso,
                    url=str(it.get("url_html", "") or ""),
                    metadata={
                        "identificador": it.get("identificador", ""),
                        "provincia": it.get("titulo", ""),
                        "seccion": it.get("seccion", "A"),
                        "actos": comp["acts"],
                    },
                    entity=target,
                    query=target,
                ))
                if len(results) >= count:
                    return results
    return results


def search_borme(
    target: str,
    target_type: str,
    date_from: str,
    date_to: str,
    count: int | None = None,
) -> tuple[list[dict] | None, str | None]:
    if not REQUESTS_AVAILABLE:
        return None, "requests library not available"
    if not target:
        return None, "el parámetro 'target' es obligatorio"
    if target_type in ("nif", "cif"):
        return None, (
            "BORME no publica NIF/CIF (enmascarados por protección de datos); "
            "busque por nombre de empresa."
        )
    if count is None or count <= 0:
        count = DEFAULT_COUNT
    dates = date_range(date_from, date_to)
    if len(dates) > BORME_MAX_RANGE_DAYS:
        return None, f"date range too large (max {BORME_MAX_RANGE_DAYS} days)"
    results: list[dict] = []
    for d in dates:
        day_iso = datetime.strptime(d, "%Y%m%d").strftime("%Y-%m-%d")
        items = fetch_borme_day(d)
        primary = [it for it in items if it.get("seccion") == "A"]
        secondary = [it for it in items if it.get("seccion") == "C"]
        for it in secondary:
            evs = _evidence_from_secondary_item(it, target, day_iso)
            if evs:
                results.extend(evs)
                if len(results) >= count:
                    return results[:count], None
        if len(results) >= count:
            return results[:count], None
        for ev in _search_primary_day(primary, target, count - len(results), day_iso):
            results.append(ev)
            if len(results) >= count:
                return results[:count], None
    return results[:count], None


def _render_results(target: str, results: list[dict]) -> str:
    """Render evidence results into the user-facing markdown text.

    Evidence carries section-specific metadata (never ``raw``):
    - Section A (actos inscritos): ``metadata.actos`` is a list of acts.
    - Section C (anuncios): no ``actos`` key; ``metadata.apartado`` names
      the section apartado as context.
    BORME masks NIFs by design, so no nif field is ever present.
    """
    lines = [f"**BORME — Actos mercantiles para {target}**\n"]
    for r in results:
        m = r.get("metadata", {}) or {}
        lines.append(f"Fecha: {r['date']} | {r['title']}")
        actos = m.get("actos") or []
        if actos:
            for acto in actos:
                lines.append(f"  → {acto}")
        elif m.get("apartado"):
            lines.append(f"  → Apartado: {m['apartado']}")
        lines.append("")
    if not results:
        lines.append("No se encontraron resultados.")
    return "\n".join(lines)


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
        target_type = args.get("target_type", "name")
        if target_type not in ("nif", "cif", "name"):
            target_type = "name"
        date_from = args.get("date_from", "")
        date_to = args.get("date_to", "")
        count = args.get("count", DEFAULT_COUNT)
        try:
            count = int(count)
        except (TypeError, ValueError):
            count = DEFAULT_COUNT
        if count <= 0:
            count = DEFAULT_COUNT

        results, error = search_borme(
            target, target_type, date_from, date_to, count
        )
        if error:
            write_response({"success": False, "request_id": request_id,
                            "error": {"code": "SEARCH_FAILED", "message": error}})
            return
        text = _render_results(target, results)
        write_response({
            "success": True, "request_id": request_id,
            "content": [{"type": "text", "text": text}],
            "structured_content": build_search_result(
                source="borme", target=target, results=results),
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
