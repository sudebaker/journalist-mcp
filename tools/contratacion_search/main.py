#!/usr/bin/env python3
"""PLACSP (contrataciondelsectorpublico.gob.es) search tool.

Busca licitaciones/contratos publicados en la Plataforma de Contratación del
Sector Público usando el feed ATOM (CODICE) en vivo para rangos <= 90 días y
descargas ZIP anuales para rangos mayores.

Estrategia híbrida:
  - <= 90 días  -> feed ATOM live, siguiendo paginacion rel="next" (max 20 paginas)
  - > 90 días   -> ZIP anuales {base}_{YYYY}.zip, se extraen y parsean los *.atom

Estructura CODICE (verificada en runtime):
  feed atom:entry -> cac-place-ext:ContractFolderStatus
    cbc:ContractFolderID                         -> id_licitacion
    cbc-place-ext:ContractFolderStatusCode       -> estado (PUB/RES/EV/ADJ/PRE)
    cac-place-ext:LocatedContractingParty/cac:Party/cac:PartyName/cbc:Name -> organo
    cac-place-ext:LocatedContractingParty/cac:Party/cbc:PartyIdentification/cbc:ID[@schemeName=NIF]
    cac:WinningParty/cac:PartyName/cbc:Name      -> adjudicatario
    cac:WinningParty/cbc:PartyIdentification/cbc:ID[@schemeName=NIF] -> nif adjudicatario
    cac:ProcurementProject/cac:BudgetAmount/cbc:EstimatedOverallContractAmount -> importe

No hay WAF ni CAPTCHA: el feed devuelve XML limpio (verificado por curl).
"""
import json
import os
import re
import shutil
import sys
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common.structured_logging import get_logger
from common.http import request_with_retry, REQUESTS_AVAILABLE
from common.evidence import build_evidence
from common.entity_normalizer import normalize_for_match

logger = get_logger(__name__, "contratacion_search")

LIVE_FEED = (
    "https://contrataciondelsectorpublico.gob.es/sindicacion/"
    "sindicacion_643/licitacionesPerfilesContratanteCompleto3.atom"
)
ZIP_BASE = (
    "https://contrataciondelsectorpublico.gob.es/sindicacion/"
    "sindicacion_643/licitacionesPerfilesContratanteCompleto3"
)
SOURCE = "CONTRATACION"
DEFAULT_LOOKBACK_DAYS = 30
DEFAULT_COUNT = 20
MAX_LIVE_PAGES = int(os.environ.get("CONTRATACION_MAX_PAGES", "20"))
MAX_ZIP_YEARS = int(os.environ.get("CONTRATACION_MAX_ZIP_YEARS", "5"))
FEED_TIMEOUT = 60
ZIP_TIMEOUT = 60
ZIP_CHUNK_SIZE = 1024 * 256

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "cbc": "urn:dgpe:names:draft:codice:schema:xsd:CommonBasicComponents-2",
    "cac": "urn:dgpe:names:draft:codice:schema:xsd:CommonAggregateComponents-2",
    "cac-place-ext": "urn:dgpe:names:draft:codice-place-ext:schema:xsd:CommonAggregateComponents-2",
    "cbc-place-ext": "urn:dgpe:names:draft:codice-place-ext:schema:xsd:CommonBasicComponents-2",
}


def _parse_date(value: str) -> datetime.date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _default_window(
    date_from: str, date_to: str
) -> tuple[datetime.date, datetime.date]:
    today = datetime.now().date()
    desde = _parse_date(date_from) or (today - timedelta(days=DEFAULT_LOOKBACK_DAYS))
    hasta = _parse_date(date_to) or today
    if hasta < desde:
        hasta = today
    return desde, hasta


def _entry_matches(
    data: dict, target: str, target_type: str
) -> bool:
    """Filtra un entry parseado por target.

    nif/cif: comparación exacta en mayúsculas contra cualquiera de los NIF del
    entry (adjudicatario u órgano). Si el entry no tiene NIF -> se descarta.
    name: substring en ambos sentidos sobre organo/adjudicatario normalizados.
    """
    metadata = data.get("metadata") or {}
    if target_type in ("nif", "cif"):
        target_upper = (target or "").strip().upper()
        if not target_upper:
            return False
        nifs = metadata.get("nifs") or []
        if not nifs:
            return False
        return target_upper in nifs
    target_norm = normalize_for_match(target)
    if not target_norm:
        return False
    candidates = [
        metadata.get("organo_contratacion") or "",
        metadata.get("adjudicatario_nombre") or "",
    ]
    for name in candidates:
        name_norm = normalize_for_match(name)
        if not name_norm:
            continue
        if target_norm in name_norm or name_norm in target_norm:
            return True
    return False


def _extract_party_name(container) -> str:
    if container is None:
        return ""
    name_el = container.find("cac:PartyName/cbc:Name", NS)
    if name_el is not None and name_el.text:
        return name_el.text.strip()
    return ""


def _collect_nifs(entry) -> list[str]:
    nifs: list[str] = []
    for pid in entry.iter(f"{{{NS['cbc']}}}ID"):
        if (pid.get("schemeName") or "").upper() == "NIF" and pid.text:
            nif = pid.text.strip()
            if nif and nif not in nifs:
                nifs.append(nif)
    return nifs


def _parse_entry(entry, query: str) -> dict:
    """Parsea un <entry> del feed CODICE en un Evidence dict."""
    title = entry.findtext("atom:title", "", NS) or ""
    summary = entry.findtext("atom:summary", "", NS) or ""
    updated = entry.findtext("atom:updated", "", NS) or ""
    date = updated[:10]

    url = ""
    for rel in ("alternate", None):
        lk = entry.find(f'atom:link[@rel="{rel}"]', NS) if rel else entry.find("atom:link", NS)
        if lk is not None and lk.get("href"):
            url = lk.get("href", "")
            break
    if not url:
        for lk in entry.findall("atom:link", NS):
            if lk.get("href"):
                url = lk.get("href", "")
                break

    cfs = entry.find("cac-place-ext:ContractFolderStatus", NS)
    id_licitacion = ""
    estado = ""
    organo = ""
    adjudicatario = ""
    importe_estimado = ""
    nifs: list[str] = []
    if cfs is not None:
        id_el = cfs.find("cbc:ContractFolderID", NS)
        if id_el is not None and id_el.text:
            id_licitacion = id_el.text.strip()
        st_el = cfs.find("cbc-place-ext:ContractFolderStatusCode", NS)
        if st_el is not None and st_el.text:
            estado = st_el.text.strip()

        lcp = cfs.find("cac-place-ext:LocatedContractingParty", NS)
        if lcp is not None:
            party = lcp.find("cac:Party", NS)
            organo = _extract_party_name(party)

        wp = cfs.find("cac:WinningParty", NS)
        if wp is not None:
            adjudicatario = _extract_party_name(wp)

        est_el = cfs.find(
            ".//cbc:EstimatedOverallContractAmount", NS
        )
        if est_el is None:
            est_el = cfs.find(".//cbc-place-ext:EstimatedAmount", NS)
        if est_el is not None and est_el.text:
            importe_estimado = est_el.text.strip()

        nifs = _collect_nifs(entry)

    metadata = {
        "id_licitacion": id_licitacion,
        "organo_contratacion": organo,
        "estado": estado,
        "importe_estimado": importe_estimado,
        "adjudicatario_nombre": adjudicatario,
        "adjudicatario_nif": next((n for n in nifs if n), ""),
        "nifs": nifs,
        "resumen": summary[:500],
    }
    return build_evidence(
        source=SOURCE,
        official=True,
        confidence=1.0,
        title=title,
        date=date,
        url=url,
        metadata=metadata,
        query=query,
    )


def _search_live_atom(
    target: str,
    target_type: str,
    desde: datetime.date,
    hasta: datetime.date,
    count: int,
) -> tuple[list[dict] | None, str | None]:
    """Crawlea el feed ATOM live siguiendo paginacion rel="next"."""
    if not REQUESTS_AVAILABLE:
        return None, "la librería 'requests' no está disponible"

    results: list[dict] = []
    feed_url = LIVE_FEED
    pages = 0
    query_label = f"{target_type}:{target}"

    while feed_url and pages < MAX_LIVE_PAGES:
        try:
            resp = request_with_retry(
                "GET",
                feed_url,
                headers={
                    "Accept": "application/atom+xml, application/xml, text/xml, */*"
                },
                timeout=FEED_TIMEOUT,
                max_retries=2,
            )
        except Exception as exc:
            return None, f"error de red al obtener el feed PLACSP: {exc}"

        if resp.status_code != 200:
            return None, (
                f"el feed PLACSP devolvió estado {resp.status_code} "
                f"(URL: {feed_url})"
            )

        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError as exc:
            return None, f"no se pudo parsear el feed ATOM: {exc}"

        page_max_date: datetime.date | None = None
        for entry in root.findall("atom:entry", NS):
            data = _parse_entry(entry, query_label)
            d = _parse_date(data.get("date") or "")
            if d is not None and (page_max_date is None or d > page_max_date):
                page_max_date = d
            if d is not None and (d < desde or d > hasta):
                continue
            if _entry_matches(data, target, target_type):
                results.append(data)
                if len(results) >= count:
                    return results, None

        pages += 1
        if len(results) >= count:
            return results, None

        # Si lo más reciente de esta página ya es anterior a 'desde',
        # las siguientes páginas (más antiguas) están fuera del rango.
        if page_max_date is not None and page_max_date < desde:
            break

        next_link = root.find('atom:link[@rel="next"]', NS)
        if next_link is None or not next_link.get("href"):
            break
        feed_url = next_link.get("href", "")
        if feed_url == LIVE_FEED:
            break

    return results, None


def _search_zip_files(
    target: str,
    target_type: str,
    desde: datetime.date,
    hasta: datetime.date,
    count: int,
) -> tuple[list[dict] | None, str | None]:
    """Descarga los ZIP anuales del rango y parsea los *.atom incluidos.

    Descarga en streaming a un fichero temporal en disco para no cargar el
    ZIP completo en memoria (los ZIP anuales pueden superar los 500 MB).
    """
    if not REQUESTS_AVAILABLE:
        return None, "la librería 'requests' no está disponible"

    years = sorted(range(desde.year, hasta.year + 1))
    if len(years) > MAX_ZIP_YEARS:
        return None, (
            f"Rango demasiado amplio: {len(years)} años (máx {MAX_ZIP_YEARS}). "
            "Use fechas más próximas."
        )

    results: list[dict] = []
    query_label = f"{target_type}:{target}"
    tmpdir = tempfile.mkdtemp(prefix="placsp_")
    try:
        for year in years:
            if len(results) >= count:
                break
            zip_url = f"{ZIP_BASE}_{year}.zip"
            zip_path = os.path.join(tmpdir, f"{year}.zip")
            logger.info(
                "Descargando ZIP PLACSP",
                extra_data={"url": zip_url, "year": year},
            )
            try:
                resp = request_with_retry(
                    "GET",
                    zip_url,
                    headers={"Accept": "application/zip, application/octet-stream, */*"},
                    timeout=ZIP_TIMEOUT,
                    max_retries=2,
                    stream=True,
                )
            except Exception as exc:
                logger.warning(
                    "Fallo al descargar ZIP",
                    extra_data={"url": zip_url, "error": str(exc)},
                )
                continue

            if resp is None or resp.status_code != 200:
                logger.warning(
                    "ZIP no disponible",
                    extra_data={"url": zip_url, "status_code": getattr(resp, "status_code", None)},
                )
                continue

            try:
                with open(zip_path, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=ZIP_CHUNK_SIZE):
                        if chunk:
                            fh.write(chunk)
            except (OSError, Exception) as exc:
                logger.warning(
                    "Fallo al guardar ZIP",
                    extra_data={"url": zip_url, "error": str(exc)},
                )
                continue
            finally:
                resp.close()

            try:
                with zipfile.ZipFile(zip_path) as zf:
                    for fname in zf.namelist():
                        if not fname.lower().endswith(".atom"):
                            continue
                        try:
                            with zf.open(fname) as fh:
                                tree = ET.parse(fh)
                        except (ET.ParseError, OSError) as exc:
                            logger.warning(
                                "No se pudo parsear ATOM del ZIP",
                                extra_data={"file": fname, "error": str(exc)},
                            )
                            continue
                        for entry in tree.getroot().findall("atom:entry", NS):
                            data = _parse_entry(entry, query_label)
                            d = _parse_date(data.get("date") or "")
                            if d is not None and (d < desde or d > hasta):
                                continue
                            if _entry_matches(data, target, target_type):
                                results.append(data)
                                if len(results) >= count:
                                    return results, None
            except zipfile.BadZipFile:
                logger.warning(
                    "ZIP corrupto", extra_data={"url": zip_url}
                )
                continue
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    return results, None


def search_contratacion(
    target: str,
    target_type: str,
    date_from: str,
    date_to: str,
    count: int,
) -> tuple[list[dict] | None, str | None]:
    """Elige estrategia híbrida según la longitud del rango de fechas."""
    if not REQUESTS_AVAILABLE:
        return None, "la librería 'requests' no está disponible"
    if not target:
        return None, "el parámetro 'target' es obligatorio"
    if count is None or count <= 0:
        count = DEFAULT_COUNT

    desde, hasta = _default_window(date_from, date_to)
    span_days = (hasta - desde).days
    logger.info(
        "Estrategia PLACSP",
        extra_data={
            "days": span_days,
            "desde": desde.isoformat(),
            "hasta": hasta.isoformat(),
            "strategy": "live" if span_days <= 90 else "zip",
        },
    )
    if span_days <= 90:
        return _search_live_atom(target, target_type, desde, hasta, count)
    return _search_zip_files(target, target_type, desde, hasta, count)


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
                "error": {
                    "code": "MISSING_TARGET",
                    "message": "el parámetro 'target' es obligatorio",
                },
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
        if count <= 0:
            count = DEFAULT_COUNT

        results, error = search_contratacion(
            target, target_type, date_from, date_to, count
        )
        if error:
            write_response({
                "success": False,
                "request_id": request_id,
                "error": {"code": "SEARCH_FAILED", "message": error},
            })
            return

        retrieved_at = datetime.now(timezone.utc).isoformat()
        desde, hasta = _default_window(date_from, date_to)

        lines = [
            f"**Contratación Pública (PLACSP) — Resultados para \"{target}\" "
            f"({target_type})**\n",
            f"Rango de fechas: {desde.isoformat()} a {hasta.isoformat()}.\n",
        ]
        if not results:
            lines.append("No se encontraron resultados.\n")
        else:
            for i, r in enumerate(results, 1):
                m = r.get("metadata", {}) or {}
                importe = m.get("importe_estimado") or ""
                lines.append(f"{i}. **{r.get('title')}**")
                lines.append(f"   - Fecha: {r.get('date') or 'N/D'}")
                if importe:
                    lines.append(f"   - Importe estimado: {importe} €")
                if m.get("adjudicatario_nombre"):
                    lines.append(
                        f"   - Adjudicatario: {m['adjudicatario_nombre']}"
                    )
                if m.get("estado"):
                    lines.append(f"   - Estado: {m['estado']}")
                if r.get("url"):
                    lines.append(f"   - URL: {r['url']}")
                lines.append("")

        write_response({
            "success": True,
            "request_id": request_id,
            "content": [{"type": "text", "text": "\n".join(lines)}],
            "structured_content": {
                "source": SOURCE,
                "official": True,
                "confidence": 1.0,
                "results": results,
                "count": len(results),
                "date_from": desde.isoformat(),
                "date_to": hasta.isoformat(),
                "retrieved_at": retrieved_at,
                "query": f"{target_type}:{target}",
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
