#!/usr/bin/env python3
"""BDNS (Base de Datos Nacional de Subvenciones) search tool.

Realiza búsquedas de concesiones/suvenciones en la API REST pública del SNPSAP
del Ministerio de Hacienda.

Descubrimiento en runtime (ver README del proyecto): el stub original apuntaba a
  https://www.pap.hacienda.gob.es/bdnstrans/api/consulta-beneficiarios
que devuelve HTTP 404 (ruta inexistente). El endpoint real es:

  GET https://www.pap.hacienda.gob.es/bdnstrans/api/concesiones/busqueda

Parámetros reales verificados en runtime:
  - nifCif        -> filtra por NIF/CIF del beneficiario (target_type nif/cif)
  - descripcion   -> búsqueda libre de texto en la convocatoria/ayuda (target_type name)
  - fechaDesde    -> fecha de concesión mínima, formato dd/MM/yyyy
  - fechaHasta    -> fecha de concesión máxima, formato dd/MM/yyyy
  - page          -> página (0-based)
  - pageSize      -> tamaño de página

Los hípicitos del stub (nifBeneficiario / nombreBeneficiario) son SILENCIOSAMENTE
ignorados por la API (devuelven el catálogo base). El param 'numeroIdentificacion'
que aparece en algunos ejemplos públicos también es ignorado.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common.http import request_with_retry, REQUESTS_AVAILABLE
from common.evidence import build_evidence
from common.entity_normalizer import normalize_for_match
from common.structured_logging import get_logger

logger = get_logger(__name__, "bdns_search")

BDNS_API = "https://www.pap.hacienda.gob.es/bdnstrans/api"
BDNS_URL = f"{BDNS_API}/concesiones/busqueda"

MAX_PAGES = 20  # anti-infinite-loop
PAGE_SIZE_CAP = 100
DEFAULT_COUNT = 10
# La búsqueda por texto (descripcion) escanea ~28M de registros del lado del
# servidor para calcular totalElements; puede tardar 15-40s. 45s da margen de
# seguridad sin superar el límite de proceso de 120s con reintentos.
PAGE_TIMEOUT = 45


def _fmt_bdns_date(date_str: str) -> str | None:
    """Convierte YYYY-MM-DD -> dd/MM/yyyy para la API BDNS. None si está vacío o es inválido."""
    if not date_str:
        return None
    try:
        d = datetime.strptime(date_str.strip(), "%Y-%m-%d")
    except ValueError:
        logger.warning(
            "Formato de fecha invalido (se ignora filtro)",
            extra_data={"date_str": str(date_str)},
        )
        return None
    return d.strftime("%d/%m/%Y")


def _default_window(date_from: str, date_to: str) -> tuple[str, str]:
    """Rellena fechas por defecto (~5 años atrás -> hoy). Devuelve (from, to) en YYYY-MM-DD."""
    now = datetime.now(timezone.utc)
    end = date_to or now.strftime("%Y-%m-%d")
    start = date_from or (now - timedelta(days=365 * 5)).strftime("%Y-%m-%d")
    return start, end


def _build_params(target: str, target_type: str, date_from: str, date_to: str) -> dict[str, Any]:
    """Construye los query params verificados en runtime para la API BDNS."""
    df, dt = _default_window(date_from, date_to)
    params: dict[str, Any] = {
        "fechaDesde": _fmt_bdns_date(df),
        "fechaHasta": _fmt_bdns_date(dt),
    }
    # target_type nif/cif -> nifCif (verificado: filtra por NIF/CIF exacto)
    # target_type name    -> descripcion (verificado: busqueda libre de texto)
    if target_type in ("nif", "cif"):
        params["nifCif"] = target
    else:
        params["descripcion"] = target
    return {k: v for k, v in params.items() if v is not None}


def _item_to_evidence(item: dict, query: str, norm_target: str, target_type: str) -> dict:
    """Mapea un ítem JSON de la API BDNS a un Evidence estructurado."""
    beneficiario = (item.get("beneficiario") or "").strip()
    convocatoria = (item.get("convocatoria") or "").strip()
    fecha = (item.get("fechaConcesion") or "").strip()
    url_br = (item.get("urlBR") or "").strip()
    cod_concesion = (item.get("codConcesion") or "").strip()
    bdns_id = item.get("id")

    if beneficiario:
        title = beneficiario
    elif convocatoria:
        title = convocatoria
    else:
        title = f"Concesión BDNS {cod_concesion or bdns_id or ''}".strip()

    # URL canónica: se prioriza urlBR (documento base/regulador); si falta, se
    # construye una URL de consulta a la ficha BDNS.
    canonical_url = url_br or (
        f"https://www.pap.hacienda.gob.es/bdnstrans/GE/es/concesiones/consulta?id={bdns_id}"
        if bdns_id is not None else ""
    )

    metadata = {
        "codConcesion": cod_concesion,
        "numeroConvocatoria": item.get("numeroConvocatoria"),
        "idConvocatoria": item.get("idConvocatoria"),
        "instrumento": item.get("instrumento"),
        "importe": item.get("importe"),
        "ayudaEquivalente": item.get("ayudaEquivalente"),
        "nivel1": item.get("nivel1"),
        "nivel2": item.get("nivel2"),
        "nivel3": item.get("nivel3"),
        "idPersona": item.get("idPersona"),
        "fechaAlta": item.get("fechaAlta"),
        "urlBR": url_br,
        "bdns_id": bdns_id,
    }

    # Uso genuíno de normalize_for_match: para búsquedas por nombre, marcar en
    # metadata si el beneficiario devuelto coincide (normalizado) con el nombre
    # buscado. En búsquedas por nombre, el beneficiario de personas físicas viene
    # enmascarado (***0461** NOMBRE), por lo que el match puede ser partial.
    if target_type == "name":
        norm_benef = normalize_for_match(beneficiario)
        metadata["beneficiary_name_match"] = bool(norm_target) and (norm_target in (norm_benef or ""))
        metadata["normalized_query"] = norm_target

    return build_evidence(
        source="BDNS",
        official=True,
        confidence=1.0,
        title=title,
        date=fecha,
        url=canonical_url,
        metadata=metadata,
        query=query,
    )


def search_bdns(
    target: str,
    target_type: str,
    date_from: str,
    date_to: str,
    count: int,
) -> tuple[list[dict], str | None]:
    """Busca concesiones en BDNS.

    Devuelve (results, error). Si error es None, results es una lista de Evidence.
    """
    if not REQUESTS_AVAILABLE:
        return None, "la librería 'requests' no está disponible"
    if not target:
        return None, "el parámetro 'target' es obligatorio"
    if count is None or count <= 0:
        count = DEFAULT_COUNT

    df, dt = _default_window(date_from, date_to)
    params = _build_params(target, target_type, date_from, date_to)

    query_label = f"{target_type}:{target}"
    norm_target = normalize_for_match(target)

    page_size = min(max(count, 1), PAGE_SIZE_CAP)
    results: list[dict] = []
    page = 0
    pages_fetched = 0

    while pages_fetched < MAX_PAGES:
        params["page"] = page
        params["pageSize"] = page_size

        logger.info(
            "Petición BDNS",
            extra_data={"url": BDNS_URL, "page": page, "pageSize": page_size},
        )

        try:
            resp = request_with_retry(
                "GET",
                BDNS_URL,
                params=params,
                headers={"Accept": "application/json"},
                timeout=PAGE_TIMEOUT,
                max_retries=1,
            )
        except Exception as exc:
            return None, f"error de red al consultar BDNS: {exc}"

        # CRITICAL: si la API devuelve 400 (p.ej. params equivocados), registrar
        # la URL efectiva y el cuerpo para poder adaptar los nombres de parámetro.
        if resp.status_code != 200:
            body = resp.text[:1000]
            logger.error(
                "Error de la API BDNS",
                extra_data={
                    "status_code": resp.status_code,
                    "effective_url": resp.url,
                    "body": body,
                    "sent_params": params,
                },
            )
            return None, (
                f"la API de BDNS devolvió estado {resp.status_code}. "
                f"URL efectiva: {resp.url}. Cuerpo: {body[:200]}"
            )

        try:
            data = resp.json()
        except ValueError:
            logger.error(
                "Respuesta BDNS no JSON",
                extra_data={
                    "status_code": resp.status_code,
                    "effective_url": resp.url,
                    "body": resp.text[:1000],
                    "sent_params": params,
                },
            )
            return None, "la API de BDNS devolvió una respuesta que no es JSON"

        content = data.get("content") or []
        if isinstance(content, dict):
            content = [content]

        for item in content:
            results.append(
                _item_to_evidence(item, query_label, norm_target, target_type)
            )
            if len(results) >= count:
                break

        pages_fetched += 1

        if len(results) >= count:
            break
        # Parada: última página o sin contenido
        if data.get("last") is True or not content:
            break
        total_pages = data.get("totalPages", 0)
        if total_pages and page >= total_pages - 1:
            break
        page += 1

    return results, None


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
            write_response({
                "success": False,
                "request_id": request_id,
                "error": {"code": "MISSING_TARGET", "message": "el parámetro 'target' es obligatorio"},
            })
            return
        target_type = args.get("target_type", "nif")
        if target_type not in ("nif", "cif", "name"):
            target_type = "nif"
        date_from = args.get("date_from", "")
        date_to = args.get("date_to", "")
        count = args.get("count", DEFAULT_COUNT)
        try:
            count = int(count)
        except (TypeError, ValueError):
            count = DEFAULT_COUNT

        results, error = search_bdns(target, target_type, date_from, date_to, count)
        if error:
            write_response({
                "success": False,
                "request_id": request_id,
                "error": {"code": "SEARCH_FAILED", "message": error},
            })
            return

        retrieved_at = datetime.now(timezone.utc).isoformat()
        df, dt = _default_window(date_from, date_to)

        lines = [
            f"**BDNS — Concesiones para \"{target}\" ({target_type})**\n",
            f"Rango de fecha de concesión: {df} a {dt}.\n",
        ]
        if not results:
            lines.append("No se encontraron concesiones que coincidan con los criterios.\n")
        else:
            for r in results:
                ev = r
                m = ev.get("metadata", {})
                lines.append(f"- Fecha: {ev.get('date') or 'N/D'}")
                lines.append(f"  **{ev.get('title')}**")
                if m.get("importe") is not None:
                    lines.append(f"  Importe: {m['importe']} €")
                if m.get("instrumento"):
                    lines.append(f"  Instrumento: {m['instrumento'].strip()}")
                lines.append(f"  Concesión: {m.get('codConcesion') or 'N/D'}")
                if m.get("urlBR"):
                    lines.append(f"  Documento: {m['urlBR']}")
                lines.append("")

        write_response({
            "success": True,
            "request_id": request_id,
            "content": [{"type": "text", "text": "\n".join(lines)}],
            "structured_content": {
                "source": "BDNS",
                "target": target,
                "official": True,
                "confidence": 1.0,
                "evidence_type": "official_record",
                "results": results,
                "count": len(results),
                "date_from": df,
                "date_to": dt,
                "retrieved_at": retrieved_at,
                "query": f"{target_type}:{target}",
                "endpoint": BDNS_URL,
            },
        })
    except json.JSONDecodeError:
        write_response({
            "success": False,
            "request_id": "",
            "error": {"code": "INVALID_JSON", "message": "No se pudo analizar el JSON de entrada"},
        })
    except Exception as exc:
        logger.error("Excepción no gestionada", extra_data={"error": str(exc)})
        write_response({
            "success": False,
            "request_id": request.get("request_id", ""),
            "error": {"code": "EXECUTION_FAILED", "message": str(exc)},
        })


if __name__ == "__main__":
    main()
