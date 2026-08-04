#!/usr/bin/env python3
"""DOUE (EUR-Lex) SOAP search tool.

Consulta el WebService SOAP de EUR-Lex (Diario Oficial de la Unión Europea)
para localizar documentos que mencionan un NIF/CIF o nombre.

Verificado contra la WSDL real y la documentación oficial (Web Service User
Manual v2.01). Salientamente diferente del contexto inicial del plan:

  * Operación: ``doQuery`` (no ``GetNoticeList``). SOAPAction:
    ``https://eur-lex.europa.eu/ws/doQuery``.
  * SOAP 1.2: el sobre lleva ``xmlns="http://www.w3.org/2003/05/soap-envelope"``.
    Un sobre SOAP 1.1 produce un fault ``VersionMismatch``.
  * Raíz del cuerpo: ``searchRequest`` (elemento global ``elx:searchRequest``),
    con namespace cualificado ``http://eur-lex.europa.eu/search``. No se envuelve
    en un elemento ``doQuery``.
  * WS-Security UsernameToken (PasswordText) es obligatorio: sin credenciales
    el servicio responde ``wsse:InvalidSecurity`` (código 1000). Las credenciales
    provienen del registro EUR-Lex (usuario ECAS + contraseña) y se leen de las
    variables de entorno ``EUR_LEX_USERNAME`` / ``EUR_LEX_PASSWORD``.

Sintaxis expert query (manual oficial): ``DN`` = número de documento (CELEX),
``DD`` = fecha de documento con formato ``DD/MM/YYYY``.
"""

import json
import os
import sys
from datetime import datetime
from typing import Any
from xml.sax.saxutils import escape as xml_escape

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common.structured_logging import get_logger  # noqa: E402
from common.http import request_with_retry, REQUESTS_AVAILABLE  # noqa: E402
from common.evidence import build_evidence  # noqa: E402

logger = get_logger(__name__, "doue_search")

WSDL_URL = "https://eur-lex.europa.eu/EURLexWebService"
SOAPACTION = "https://eur-lex.europa.eu/ws/doQuery"
TARGET_NS = "http://eur-lex.europa.eu/search"
SEARCH_LANG = os.environ.get("EUR_LEX_LANG", "es")
MAX_PAGE_SIZE = 100


def format_date(date_str: str) -> str:
    """Convierte YYYY-MM-DD al formato DD/MM/YYYY exigido por EUR-Lex."""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        return date_str
    return dt.strftime("%d/%m/%Y")


def _escape_query(value: str) -> str:
    """Escapa un valor para incrustarlo de forma segura en la expert query.

    La expert query usada por EUR-Lex no es XML, pero se escapa para evitar que
    comillas o caracteres especiales rompan la expresión.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"')


def build_expert_query(target: str, target_type: str,
                       date_from: str, date_to: str) -> str:
    """Construye la expert query de EUR-Lex.

    Nota: ``DN`` es el número de documento (CELEX). Para búsquedas por nombre
    de entidad, EUR-Lex recomienda texto completo (``Text ~ "nombre"``) ya que
    DN no indexa nombres. Se expone ``target_type`` para permitir adaptaciones.
    """
    if target_type in ("nif", "cif"):
        target_expr = f"DN={_escape_query(target)}"
    else:
        target_expr = f'DN="{_escape_query(target)}"'
    parts = [target_expr]
    if date_from:
        parts.append(f"DD >= {format_date(date_from)}")
    if date_to:
        parts.append(f"DD <= {format_date(date_to)}")
    return " AND ".join(parts)


def build_soap_envelope(expert_query: str, page: int, page_size: int,
                        lang: str, username: str, password: str) -> str:
    """Construye el sobre SOAP 1.2 con WS-Security UsernameToken.

    Se escapa manualmente el contenido con ``xml.sax.saxutils.escape`` (no
    se emplea zeep, no disponible como dependencia).
    """
    q = xml_escape(expert_query)
    if username and password:
        security_block = (
            '<wsse:Security soap:mustUnderstand="true">'
            '<wsse:UsernameToken wsu:Id="UsernameToken-1">'
            f"<wsse:Username>{xml_escape(username)}</wsse:Username>"
            '<wsse:Password Type="http://docs.oasis-open.org/wss/'
            "2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordText\">"
            f"{xml_escape(password)}</wsse:Password>"
            "</wsse:UsernameToken>"
            "</wsse:Security>"
        )
    else:
        security_block = ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/'
        'oasis-200401-wss-wssecurity-secext-1.0.xsd" '
        'xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/'
        'oasis-200401-wss-wssecurity-utility-1.0.xsd" '
        f'xmlns:sear="{TARGET_NS}">'
        f"<soap:Header>{security_block}</soap:Header>"
        "<soap:Body>"
        '<sear:searchRequest>'
        f"<sear:expertQuery>{q}</sear:expertQuery>"
        f"<sear:page>{page}</sear:page>"
        f"<sear:pageSize>{page_size}</sear:pageSize>"
        f"<sear:searchLanguage>{xml_escape(lang)}</sear:searchLanguage>"
        "<sear:excludeAllConsleg>false</sear:excludeAllConsleg>"
        "<sear:limitToLatestConsleg>false</sear:limitToLatestConsleg>"
        "</sear:searchRequest>"
        "</soap:Body>"
        "</soap:Envelope>"
    )


def _ns_strip(tag: str) -> str:
    """Devuelve el nombre local de una etiqueta XML sin namespace."""
    return tag.rsplit("}", 1)[-1]


def _find_text(root, *local_names):
    """Busca recursivamente el primer elemento cuyo nombre local coincida y
    devuelve su texto (o el texto de su hijo VALUE/value)."""
    wanted = {n.lower() for n in local_names}
    for el in root.iter():
        if _ns_strip(el.tag).lower() in wanted:
            if el.text and el.text.strip():
                return el.text.strip()
            for child in el:
                if _ns_strip(child.tag).lower() in ("value", "valor"):
                    if child.text and child.text.strip():
                        return child.text.strip()
    return ""


def _find_list(root, local_name):
    """Devuelve la lista de textos de todos los elementos con ese nombre local."""
    wanted = local_name.lower()
    out = []
    for el in root.iter():
        if _ns_strip(el.tag).lower() == wanted:
            txt = el.text.strip() if el.text else ""
            if not txt:
                for child in el:
                    if _ns_strip(child.tag).lower() in ("value", "valor"):
                        txt = (child.text or "").strip()
                        break
            if txt:
                out.append(txt)
    return out


def parse_response(xml_text: str, lang: str) -> list[dict]:
    """Parsea la respuesta SOAP y extrae los avisos."""
    try:
        from xml.etree import ElementTree as ET
    except ImportError:
        return []

    root = ET.fromstring(xml_text)

    # Detectar fallos SOAP antes de parsear resultados.
    for fault in root.iter():
        if _ns_strip(fault.tag).lower() == "fault":
            reason = ""
            for child in fault.iter():
                tag = _ns_strip(child.tag).lower()
                if tag in ("reason", "message", "text") and child.text:
                    reason = child.text.strip()
            return [{"_soap_fault": True, "fault": reason or "SOAP Fault"}]

    results = []
    for result in root.iter():
        if _ns_strip(result.tag).lower() != "result":
            continue

        celex = _find_text(result, "ID_CELEX", "RESOURCE_LEGAL_ID_CELEX",
                           "RESOURCE_LEGAL_NUMBER_NATURAL_CELEX")
        title = _find_text(result, "EXPRESSION_TITLE", "WORK_TITLE", "TITLE")
        document_type = _find_text(
            result, "DTS_SUBDOM", "RESOURCE_LEGAL_TYPE", "TYPE")
        pub_date = _find_text(
            result, "OFFICIAL-JOURNAL-ACT_DATE_PUBLICATION",
            "WORK_DATE_DOCUMENT", "PUBLICATION_DATE", "DATE_PUBLICATION",
            "LASTMODIFICATIONDATE", "DD")
        doc_links = _find_list(result, "document_link") or _find_list(
            result, "CONTENT_URL")
        url = doc_links[0] if doc_links else ""

        # Referencia al Diario Oficial: "OJ <sección> <número> <año>".
        oj_year = _find_text(result, "OFFICIAL-JOURNAL-ACT_YEAR",
                             "OFFICIAL-JOURNAL_YEAR")
        oj_number = _find_text(result, "OFFICIAL-JOURNAL-ACT_NUMBER",
                               "OFFICIAL-JOURNAL_NUMBER")
        oj_section = _find_text(result, "OFFICIAL-JOURNAL-ACT_SECTION_OJ",
                                "SECTION_OJ")
        oj_ref = ""
        if oj_year or oj_number or oj_section:
            oj_ref = " ".join(
                p for p in ("OJ", oj_section, oj_number, oj_year) if p)

        if not url and celex:
            url = (f"https://eur-lex.europa.eu/legal-content/{lang}/"
                   f"TXT/?uri=CELEX:{celex}")

        results.append({
            "celex": celex,
            "title": title,
            "documentType": document_type,
            "publicationDate": pub_date,
            "ojReference": oj_ref,
            "url": url,
        })

    return results


def search_doue(target: str, target_type: str, date_from: str, date_to: str,
                count: int, lang: str = SEARCH_LANG):
    """Ejecuta la búsqueda en el WebService SOAP de EUR-Lex."""
    if not REQUESTS_AVAILABLE:
        return None, "requests library not available"

    username = os.environ.get("EUR_LEX_USERNAME", "")
    password = os.environ.get("EUR_LEX_PASSWORD", "")
    if not username or not password:
        return None, (
            "Credenciales EUR-Lex no configuradas. Defina las variables de "
            "entorno EUR_LEX_USERNAME y EUR_LEX_PASSWORD (registro EUR-Lex / "
            "ECAS). El WebService exige WS-Security UsernameToken."
        )

    if not target.strip():
        return None, "target is required"
    count = max(1, min(int(count or 20), 1000))
    page_size = min(count, MAX_PAGE_SIZE)

    query = build_expert_query(target, target_type, date_from, date_to)
    logger.info("DOUE expert query", extra_data={
        "query": query, "target": target, "target_type": target_type})

    headers = {
        "Content-Type": "application/soap+xml; charset=utf-8",
        "SOAPAction": SOAPACTION,
        "Accept": "application/soap+xml, text/xml, text/plain",
    }

    notices: list[dict] = []
    page = 1
    fetched = 0
    total_hits = 0
    while fetched < count:
        envelope = build_soap_envelope(
            query, page, page_size, lang, username, password)
        try:
            resp = request_with_retry(
                "POST", WSDL_URL, headers=headers, data=envelope.encode("utf-8"),
                timeout=30, max_retries=2)
        except Exception as exc:
            logger.error("HTTP request failed",
                         extra_data={"error": str(exc)})
            return None, f"SOAP request failed: {exc}"

        if resp is None or getattr(resp, "status_code", None) != 200:
            status = getattr(resp, "status_code", None)
            text = resp.text if resp is not None else ""
            logger.warning("DOUE SOAP non-200", extra_data={
                "status": status, "body": text[:800]})
            fault_check = parse_response(text, lang) if text else []
            if fault_check and fault_check[0].get("_soap_fault"):
                return None, (
                    f"WSDL SOAP fault: {fault_check[0]['fault']}")
            return None, f"EUR-Lex SOAP returned HTTP {status}"

        page_notices = parse_response(resp.text, lang)
        if page_notices and page_notices[0].get("_soap_fault"):
            return None, f"WSDL SOAP fault: {page_notices[0]['fault']}"

        if page == 1:
            total_hits = _extract_total_hits(resp.text)

        for n in page_notices:
            if fetched >= count:
                break
            notices.append(n)
            fetched += 1

        if total_hits and fetched >= total_hits:
            break
        if not page_notices:
            break
        page += 1

    return notices, None


def _extract_total_hits(xml_text: str) -> int:
    try:
        from xml.etree import ElementTree as ET
        root = ET.fromstring(xml_text)
    except Exception:
        return 0
    for el in root.iter():
        if _ns_strip(el.tag).lower() == "totalhits":
            try:
                return int((el.text or "0").strip())
            except ValueError:
                return 0
    return 0


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
                            "error": {"code": "MISSING_TARGET",
                                      "message": "target is required"}})
            return
        target_type = args.get("target_type", "nif")
        date_from = args.get("date_from", "")
        date_to = args.get("date_to", "")
        count = int(args.get("count", 20))
        lang = args.get("lang", SEARCH_LANG)

        results, error = search_doue(target, target_type, date_from, date_to,
                                     count, lang)
        if error:
            write_response({"success": False, "request_id": request_id,
                            "error": {"code": "SEARCH_FAILED",
                                      "message": error}})
            return

        evidences = []
        for n in results:
            metadata = {
                "celex": n.get("celex", ""),
                "documentType": n.get("documentType", ""),
                "ojReference": n.get("ojReference", ""),
            }
            ev = build_evidence(
                source="doue",
                official=True,
                confidence=0.9,
                title=n.get("title", "") or n.get("celex", ""),
                date=n.get("publicationDate", "") or n.get("celex", ""),
                url=n.get("url", ""),
                metadata=metadata,
                query=f"EUR-Lex DOUE: {build_expert_query(target, target_type, date_from, date_to)}",
            )
            evidences.append(ev)

        lines = [f"**DOUE (EUR-Lex) — Resultados para {target}**\n"]
        if results:
            for i, n in enumerate(results, 1):
                lines.append(f"**{i}. CELEX: {n.get('celex', '')}**")
                lines.append(f"Título: {n.get('title', '')}")
                lines.append(
                    f"Tipo: {n.get('documentType', '')} | "
                    f"Publicación: {n.get('publicationDate', '')}")
                if n.get("ojReference"):
                    lines.append(f"Diario Oficial: {n.get('ojReference')}")
                if n.get("url"):
                    lines.append(f"URL: {n.get('url')}")
                lines.append("")
        else:
            lines.append("No se encontraron resultados.")

        write_response({
            "success": True, "request_id": request_id,
            "content": [{"type": "text", "text": "\n".join(lines)}],
            "structured_content": {
                "source": "doue", "target": target,
                "evidence": evidences,
                "count": len(evidences),
            },
        })
    except json.JSONDecodeError:
        write_response({"success": False, "request_id": "",
                        "error": {"code": "INVALID_JSON",
                                  "message": "Failed to parse JSON"}})
    except Exception as e:
        logger.error("Unhandled exception", extra_data={"error": str(e)})
        write_response({"success": False,
                        "request_id": request.get("request_id", ""),
                        "error": {"code": "EXECUTION_FAILED",
                                  "message": str(e)}})


if __name__ == "__main__":
    main()
