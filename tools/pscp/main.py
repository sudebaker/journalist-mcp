#!/usr/bin/env python3
"""PSCP (Plataforma de Contratación del Sector Público) Atom feed fetcher.

Downloads the official Atom feeds from contrataciondelestado.es and parses
each <entry> into a structured record suitable for journalist investigation:
expediente, título, importe, estado, órgano de contratación (con NIF),
ubicación, CPV, fecha de actualización y enlace al detalle.

Follows the same SubprocessRequest/SubprocessResponse JSON-over-stdio
contract as the rest of tools/* (see tools/ted_search/main.py for reference).
"""
import json
import os
import sys
import xml.etree.ElementTree as ET
from typing import Any, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common.structured_logging import get_logger

logger = get_logger(__name__, "pscp_fetch")

from common.http import request_with_retry, REQUESTS_AVAILABLE

# Atom / UBL namespaces used by contrataciondelestado.es feeds.
NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "cbc": "urn:dgpe:names:draft:codice:schema:xsd:CommonBasicComponents-2",
    "cac": "urn:dgpe:names:draft:codice:schema:xsd:CommonAggregateComponents-2",
    "cbcpe": "urn:dgpe:names:draft:codice-place-ext:schema:xsd:CommonBasicComponents-2",
    "cacpe": "urn:dgpe:names:draft:codice-place-ext:schema:xsd:CommonAggregateComponents-2",
}

FEEDS: dict[str, str] = {
    # Licitaciones completas publicadas en perfiles de contratantes.
    "licitaciones": (
        "https://contrataciondelestado.es/sindicacion/sindicacion_643/"
        "licitacionesPerfilesContratanteCompleto3.atom"
    ),
    # Plataformas agregadas — contratos menores excluidos.
    "menores": (
        "https://contrataciondelestado.es/sindicacion/sindicacion_1143/"
        "PlataformasAgregadasSinMenores.atom"
    ),
}

DEFAULT_LIMIT = 25
MAX_LIMIT = 100
HTTP_TIMEOUT = 30


def _find_text(elem: Optional[ET.Element], path: str) -> str:
    """Return the .text of the first matching descendant, or '' if missing."""
    if elem is None:
        return ""
    found = elem.find(path, NS)
    if found is None or found.text is None:
        return ""
    return found.text.strip()


def _parse_party(party_elem: Optional[ET.Element]) -> dict[str, str]:
    """Extract name, NIF, contact, address from a cac:Party element."""
    out: dict[str, str] = {
        "name": "",
        "nif": "",
        "platform_id": "",
        "website": "",
        "email": "",
        "phone": "",
        "city": "",
        "postal_code": "",
        "address": "",
        "country": "",
    }
    if party_elem is None:
        return out
    out["name"] = _find_text(party_elem, "cac:PartyName/cbc:Name")
    for ident in party_elem.findall("cac:PartyIdentification/cbc:ID", NS):
        scheme = (ident.get("schemeName") or "").upper()
        val = (ident.text or "").strip()
        if scheme == "NIF":
            out["nif"] = val
        elif scheme == "ID_PLATAFORMA":
            out["platform_id"] = val
    out["website"] = _find_text(party_elem, "cac:WebsiteURI")
    out["email"] = _find_text(party_elem, "cac:Contact/cbc:ElectronicMail")
    out["phone"] = _find_text(party_elem, "cac:Contact/cbc:Telephone")
    addr = party_elem.find("cac:PostalAddress", NS)
    if addr is not None:
        out["city"] = _find_text(addr, "cbc:CityName")
        out["postal_code"] = _find_text(addr, "cbc:PostalZone")
        out["address"] = _find_text(addr, "cac:AddressLine/cbc:Line")
        out["country"] = _find_text(addr, "cac:Country/cbc:Name")
    return out


def _parse_entry(entry: ET.Element) -> dict[str, Any]:
    """Parse one Atom <entry> into a JSON-friendly dict."""
    expediente = _find_text(entry, "cacpe:ContractFolderStatus/cbc:ContractFolderID")
    estado = _find_text(
        entry, "cacpe:ContractFolderStatus/cbcpe:ContractFolderStatusCode"
    )
    organo = _parse_party(
        entry.find(
            "cacpe:ContractFolderStatus/cacpe:LocatedContractingParty/cac:Party",
            NS,
        )
    )
    proj = entry.find("cacpe:ContractFolderStatus/cac:ProcurementProject", NS)
    titulo = _find_text(entry, "atom:title") or _find_text(proj, "cbc:Name")
    cpv = _find_text(
        proj, "cac:RequiredCommodityClassification/cbc:ItemClassificationCode"
    )
    location = proj.find("cac:RealizedLocation", NS) if proj is not None else None
    amount_block = proj.find("cac:BudgetAmount", NS) if proj is not None else None
    importe = (
        _find_text(amount_block, "cbc:EstimatedOverallContractAmount")
        or _find_text(amount_block, "cbc:TotalAmount")
    )
    currency = ""
    if amount_block is not None:
        for child in amount_block:
            tag = child.tag.split("}")[-1]
            if tag in ("EstimatedOverallContractAmount", "TotalAmount"):
                currency = child.get("currencyID") or ""
                if child.text and child.text.strip():
                    break
    link = ""
    link_elem = entry.find("atom:link", NS)
    if link_elem is not None:
        link = link_elem.get("href") or ""
    return {
        "expediente": expediente,
        "titulo": titulo,
        "estado": estado,
        "importe": importe,
        "currency": currency,
        "cpv": cpv,
        "organo": organo,
        "ubicacion": {
            "subentidad": _find_text(location, "cbc:CountrySubentity") if location is not None else "",
            "nuts": _find_text(location, "cbc:CountrySubentityCode") if location is not None else "",
            "ciudad": _find_text(location, "cac:Address/cbc:CityName") if location is not None else "",
        },
        "fecha_actualizacion": _find_text(entry, "atom:updated"),
        "url_detalle": link,
        "id_atom": _find_text(entry, "atom:id"),
    }


def fetch_feed(feed_type: str, limit: int) -> tuple[list[dict[str, Any]], Optional[str]]:
    """Download and parse the requested feed. Returns (entries, error)."""
    if not REQUESTS_AVAILABLE:
        return [], "requests library not available"
    url = FEEDS.get(feed_type)
    if not url:
        return [], f"unknown feed_type '{feed_type}'"
    try:
        resp = request_with_retry(
            "GET", url,
            timeout=HTTP_TIMEOUT,
            headers={
                "Accept": "application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
                "User-Agent": "journalist-mcp/pscp_fetch (+https://github.com/journalist-mcp)",
            },
        )
    except Exception as e:
        msg = str(e).lower()
        if "timeout" in msg or "timed" in msg:
            return [], "PSCP feed timed out"
        return [], f"PSCP fetch failed: {e}"
    if resp.status_code != 200:
        return [], f"PSCP feed returned HTTP {resp.status_code}"
    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as e:
        return [], f"PSCP feed parse error: {e}"
    raw_entries = root.findall("atom:entry", NS)
    parsed = [_parse_entry(e) for e in raw_entries[: max(0, limit)]]
    return parsed, None


def write_response(data: dict[str, Any]) -> None:
    print(json.dumps(data, default=str), flush=True)


def main() -> None:
    request: dict = {}
    try:
        request = json.loads(sys.stdin.read())
        request_id = request.get("request_id", "")
        args = request.get("arguments", {}) or {}
        feed_type = str(args.get("feed_type", "licitaciones")).strip() or "licitaciones"
        if feed_type not in FEEDS:
            write_response({
                "success": False,
                "request_id": request_id,
                "error": {
                    "code": "INVALID_FEED_TYPE",
                    "message": f"feed_type must be one of {sorted(FEEDS.keys())}",
                },
            })
            return
        try:
            limit = int(args.get("limit", DEFAULT_LIMIT))
        except (TypeError, ValueError):
            limit = DEFAULT_LIMIT
        limit = max(1, min(limit, MAX_LIMIT))
        results, error = fetch_feed(feed_type, limit)
        if error:
            write_response({
                "success": False,
                "request_id": request_id,
                "error": {"code": "FETCH_FAILED", "message": error},
            })
            return
        title = "Licitaciones PSCP" if feed_type == "licitaciones" else "Contratos menores (plataformas agregadas) PSCP"
        lines = [f"**{title} — {len(results)} resultados**\n"]
        for i, r in enumerate(results, 1):
            organo = r["organo"]["name"] or "(órgano desconocido)"
            nif = r["organo"]["nif"]
            nif_str = f" (NIF {nif})" if nif else ""
            importe = r["importe"] or "—"
            cur = r["currency"] or "EUR"
            lines.append(f"**{i}. {r['titulo'] or '(sin título)'}**")
            lines.append(
                f"Exp: {r['expediente']} | Estado: {r['estado']} | "
                f"Importe: {importe} {cur} | CPV: {r['cpv']}"
            )
            lines.append(f"Órgano: {organo}{nif_str}")
            loc_parts = [r["ubicacion"]["ciudad"], r["ubicacion"]["subentidad"]]
            loc = ", ".join(p for p in loc_parts if p)
            if loc:
                lines.append(f"Ubicación: {loc}")
            if r["url_detalle"]:
                lines.append(f"Detalle: {r['url_detalle']}")
            lines.append("")
        if not results:
            lines.append("No se encontraron resultados en el feed.")
        write_response({
            "success": True,
            "request_id": request_id,
            "content": [{"type": "text", "text": "\n".join(lines)}],
            "structured_content": {
                "source": "pscp",
                "feed_type": feed_type,
                "results": results,
                "count": len(results),
            },
        })
    except json.JSONDecodeError:
        write_response({
            "success": False,
            "request_id": "",
            "error": {"code": "INVALID_JSON", "message": "Failed to parse JSON"},
        })
    except Exception as e:
        logger.error("Unhandled exception", extra_data={"error": str(e)})
        write_response({
            "success": False,
            "request_id": request.get("request_id", ""),
            "error": {"code": "EXECUTION_FAILED", "message": str(e)},
        })


if __name__ == "__main__":
    main()
