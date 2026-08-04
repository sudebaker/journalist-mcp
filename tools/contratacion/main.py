#!/usr/bin/env python3
"""contratacion_fetch — Fetch licitaciones from Contratación del Sector Público.

Source: Plataforma de Contratación del Sector Público (Spain), Atom feed bundled
as a monthly ZIP:

    https://contrataciondelsectorpublico.gob.es/sindicacion/sindicacion_643/
        licitacionesPerfilesContratanteCompleto3_{YYYY}{MM}.zip

Each ZIP contains one or more Atom XML files. Each ``<entry>`` represents a
licitación/contract folder with a rich UBL/Codice extension schema (cac:Party,
cac:ProcurementProject, cbc:ContractFolderID, NIF identifiers, etc.).

This tool follows the same SubprocessResponse / stdin-JSON contract used by
sibling tools (``ted_search``, ``boe_search``, ``bdns_search``).
"""

import io
import json
import os
import re
import sys
import zipfile
from datetime import datetime, timedelta
from typing import Any, Iterable
from xml.etree import ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common.structured_logging import get_logger

logger = get_logger(__name__, "contratacion_fetch")

try:
    import requests as _requests  # type: ignore
    REQUESTS_AVAILABLE = True
except ImportError:
    _requests = None  # type: ignore
    REQUESTS_AVAILABLE = False


CONTRATACION_ZIP_URL = (
    "https://contrataciondelsectorpublico.gob.es/sindicacion/"
    "sindicacion_643/licitacionesPerfilesContratanteCompleto3_{yyyymm}.zip"
)
HTTP_TIMEOUT_SECONDS = 90

# Atom/UBL namespaces used to navigate the feed.
NS_ATOM = "{http://www.w3.org/2005/Atom}"
NS_CAC = "{urn:dgpe:names:draft:codice:schema:xsd:CommonAggregateComponents-2}"
NS_CAC_PLACE_EXT = (
    "{urn:dgpe:names:draft:codice-place-ext:schema:xsd:CommonAggregateComponents-2}"
)
NS_CBC = "{urn:dgpe:names:draft:codice:schema:xsd:CommonBasicComponents-2}"
NS_CBC_PLACE_EXT = (
    "{urn:dgpe:names:draft:codice-place-ext:schema:xsd:CommonBasicComponents-2}"
)
NS_AT_TOMBSTONES = "{http://purl.org/atompub/tombstones/1.0}"

MAX_TOTAL_ENTRIES = 20000      # safety cap on a single request


# ---------------------------------------------------------------------------
# Date / month helpers
# ---------------------------------------------------------------------------

def _validate_yyyymm(year: int, month: int) -> None:
    if not (2000 <= year <= 2100):
        raise ValueError(f"year out of range: {year}")
    if not (1 <= month <= 12):
        raise ValueError(f"month out of range: {month}")


def _parse_yyyymm(value: str) -> tuple[int, int]:
    """Parse 'YYYY', 'YYYY-MM', or 'YYYYMM' into (year, month)."""
    if not value:
        raise ValueError("empty date value")
    digits = value.replace("-", "")
    if len(digits) == 4 and digits.isdigit():
        return int(digits), 1
    if len(digits) == 6 and digits.isdigit():
        return int(digits[:4]), int(digits[4:6])
    raise ValueError(f"invalid date format: {value!r} (expected YYYY, YYYY-MM, YYYYMM)")


def _month_iter(year_from: int, month_from: int, year_to: int, month_to: int) -> Iterable[tuple[int, int]]:
    """Yield (year, month) tuples inclusive from start to end."""
    y, m = year_from, month_from
    while (y, m) <= (year_to, month_to):
        yield y, m
        if m == 12:
            y += 1
            m = 1
        else:
            m += 1


def _resolve_bundles(args: dict) -> list[tuple[int, int]]:
    """Return the list of (year, month) bundles the caller wants."""
    explicit = args.get("bundles")
    if isinstance(explicit, list) and explicit:
        out: list[tuple[int, int]] = []
        for item in explicit:
            y, m = _parse_yyyymm(str(item))
            _validate_yyyymm(y, m)
            out.append((y, m))
        return out

    if "date_range" in args and isinstance(args["date_range"], dict):
        dr = args["date_range"]
        y1, m1 = _parse_yyyymm(str(dr.get("from", "")))
        y2, m2 = _parse_yyyymm(str(dr.get("to", "")))
        _validate_yyyymm(y1, m1)
        _validate_yyyymm(y2, m2)
        if (y1, m1) > (y2, m2):
            raise ValueError("date_range.from must be <= date_range.to")
        return list(_month_iter(y1, m1, y2, m2))

    if "year" in args and "month" in args:
        y, m = int(args["year"]), int(args["month"])
        _validate_yyyymm(y, m)
        return [(y, m)]

    if "year" in args:
        y = int(args["year"])
        _validate_yyyymm(y, 1)
        return [(y, m) for m in range(1, 13)]

    raise ValueError(
        "no date input provided: pass year+month, year, date_range, or bundles"
    )


# ---------------------------------------------------------------------------
# Networking & ZIP extraction
# ---------------------------------------------------------------------------

def _download_zip(year: int, month: int) -> bytes:
    if not REQUESTS_AVAILABLE or _requests is None:
        raise RuntimeError("requests library not available")
    url = CONTRATACION_ZIP_URL.format(yyyymm=f"{year}{month:02d}")
    logger.info(
        "downloading contratacion bundle",
        extra_data={"url": url, "year": year, "month": month},
    )
    resp = _requests.get(
        url,
        timeout=HTTP_TIMEOUT_SECONDS,
        headers={
            "User-Agent": "journalist-mcp/contratacion_fetch",
            "Accept": "application/zip,application/octet-stream,*/*",
        },
        allow_redirects=True,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"contratacion HTTP {resp.status_code} for {url} "
            f"({len(resp.content)} bytes)"
        )
    return resp.content


def _zip_bodies(blob: bytes) -> list[tuple[str, bytes]]:
    """Return [(filename, body_bytes), ...] for every .atom entry in the ZIP."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile as exc:
        raise RuntimeError(f"invalid ZIP payload: {exc}") from exc
    out: list[tuple[str, bytes]] = []
    for name in zf.namelist():
        if not name.lower().endswith(".atom"):
            continue
        with zf.open(name) as fh:
            out.append((name, fh.read()))
    if not out:
        raise RuntimeError("ZIP contained no .atom entries")
    return out


# ---------------------------------------------------------------------------
# Atom parsing
# ---------------------------------------------------------------------------

def _local(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _find_child(parent: ET.Element | None, qname: str) -> ET.Element | None:
    if parent is None:
        return None
    return parent.find(qname)


def _find_children(parent: ET.Element | None, qname: str) -> list[ET.Element]:
    if parent is None:
        return []
    return parent.findall(qname)


def _text(node: ET.Element | None) -> str:
    if node is None or node.text is None:
        return ""
    return node.text.strip()


def _id_with_scheme(party: ET.Element | None, scheme: str) -> str:
    """Return the value of a cbc:ID whose schemeName attribute matches."""
    if party is None:
        return ""
    for ident in party.findall(f"{NS_CAC}PartyIdentification"):
        id_node = ident.find(f"{NS_CBC}ID")
        if id_node is None:
            continue
        if (id_node.get("schemeName") or "").upper() == scheme.upper():
            return _text(id_node)
    return ""


def _party_name(party: ET.Element | None) -> str:
    if party is None:
        return ""
    name = party.find(f"{NS_CAC}PartyName/{NS_CBC}Name")
    return _text(name)


def _walking_parents(party_block: ET.Element | None) -> list[str]:
    """Walk up the cac-place-ext:ParentLocatedParty chain collecting names."""
    parents: list[str] = []
    node = party_block.find(f"{NS_CAC_PLACE_EXT}ParentLocatedParty") if party_block is not None else None
    while node is not None:
        name = _text(node.find(f"{NS_CAC}PartyName/{NS_CBC}Name"))
        if name and name not in parents:
            parents.append(name)
        node = node.find(f"{NS_CAC_PLACE_EXT}ParentLocatedParty")
    return parents


def _parse_entry(entry: ET.Element) -> dict[str, Any]:
    """Convert one Atom <entry> into the journalist-facing record."""
    record: dict[str, Any] = {
        "id": _text(entry.find(f"{NS_ATOM}id")),
        "title": _text(entry.find(f"{NS_ATOM}title")),
        "summary": _text(entry.find(f"{NS_ATOM}summary")),
        "updated": _text(entry.find(f"{NS_ATOM}updated")),
        "url": "",
        "folder_id": "",
        "status": "",
        "contract_type": "",
        "contract_subtype": "",
        "procedure_code": "",
        "estimated_amount": None,
        "tax_exclusive_amount": None,
        "total_amount": None,
        "currency": "EUR",
        "buyer": {
            "name": "",
            "nif": "",
            "dir3": "",
            "id_plataforma": "",
            "city": "",
            "country": "",
            "email": "",
            "phone": "",
            "parent_chain": [],
        },
        "lots": [],
    }

    link = entry.find(f"{NS_ATOM}link")
    if link is not None:
        record["url"] = link.get("href", "")

    status = entry.find(
        f"{NS_CAC_PLACE_EXT}ContractFolderStatus"
    )
    if status is not None:
        record["folder_id"] = _text(status.find(f"{NS_CBC}ContractFolderID"))
        record["status"] = _text(
            status.find(f"{NS_CBC_PLACE_EXT}ContractFolderStatusCode")
        )
        party = status.find(f"{NS_CAC_PLACE_EXT}LocatedContractingParty/{NS_CAC}Party")
        buyer = record["buyer"]
        buyer["name"] = _party_name(party)
        buyer["nif"] = _id_with_scheme(party, "NIF")
        buyer["dir3"] = _id_with_scheme(party, "DIR3")
        buyer["id_plataforma"] = _id_with_scheme(party, "ID_PLATAFORMA")
        contact = party.find(f"{NS_CAC}Contact") if party is not None else None
        if contact is not None:
            buyer["phone"] = _text(contact.find(f"{NS_CBC}Telephone"))
            buyer["email"] = _text(contact.find(f"{NS_CBC}ElectronicMail"))
        address = party.find(f"{NS_CAC}PostalAddress") if party is not None else None
        if address is not None:
            buyer["city"] = _text(address.find(f"{NS_CBC}CityName"))
            buyer["country"] = _text(
                address.find(f"{NS_CAC}Country/{NS_CBC}Name")
            )
        located = status.find(f"{NS_CAC_PLACE_EXT}LocatedContractingParty")
        buyer["parent_chain"] = _walking_parents(located)

    procurement = status.find(f"{NS_CAC}ProcurementProject") if status is not None else None
    if procurement is not None:
        record["contract_type"] = _text(procurement.find(f"{NS_CBC}TypeCode"))
        record["contract_subtype"] = _text(procurement.find(f"{NS_CBC}SubTypeCode"))
        record["procedure_code"] = _text(procurement.find(f"{NS_CBC}ProcedureCode"))
        budget = procurement.find(f"{NS_CAC}BudgetAmount")
        if budget is not None:
            est = budget.find(f"{NS_CBC}EstimatedOverallContractAmount")
            if est is not None:
                record["estimated_amount"] = _amount(est)
                record["currency"] = est.get("currencyID") or "EUR"
            tax = budget.find(f"{NS_CBC}TaxExclusiveAmount")
            if tax is not None:
                record["tax_exclusive_amount"] = _amount(tax)
            tot = budget.find(f"{NS_CBC}TotalAmount")
            if tot is not None:
                record["total_amount"] = _amount(tot)
        for lot in procurement.findall(f"{NS_CAC}ProcurementProjectLot"):
            record["lots"].append(_parse_lot(lot))

    return record


def _amount(node: ET.Element) -> float | None:
    raw = _text(node)
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _parse_lot(lot: ET.Element) -> dict[str, Any]:
    lot_id = _text(lot.find(f"{NS_CBC}ID")) or _text(lot.find(f"{NS_CBC}ProcurementProjectLotID"))
    name_node = lot.find(f"{NS_CBC}Name")
    name = _text(name_node) if name_node is not None else ""
    amount = None
    budget = lot.find(f"{NS_CAC}BudgetAmount/{NS_CBC}EstimatedOverallContractAmount")
    if budget is not None:
        amount = _amount(budget)
    return {"id": lot_id, "name": name, "estimated_amount": amount}


def _iter_entries(xml_blob: bytes) -> Iterable[dict[str, Any]]:
    """Yield parsed entries from a single Atom file body."""
    try:
        root = ET.fromstring(xml_blob)
    except ET.ParseError as exc:
        logger.warning(
            "atom parse error — skipping file",
            extra_data={"error": str(exc), "size": len(xml_blob)},
        )
        return
    for entry in root.findall(f"{NS_ATOM}entry"):
        yield _parse_entry(entry)


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

def _target_haystack(record: dict[str, Any]) -> str:
    """Aggregate all text fields we will search against for a target filter."""
    buyer = record.get("buyer") or {}
    parents = " ".join(buyer.get("parent_chain") or [])
    parts = [
        record.get("title", ""),
        record.get("summary", ""),
        record.get("folder_id", ""),
        buyer.get("name", ""),
        buyer.get("nif", ""),
        buyer.get("dir3", ""),
        parents,
    ]
    return " ".join(parts)


def _matches_target(
    record: dict[str, Any],
    target: str,
    target_type: str,
) -> bool:
    if not target:
        return True
    hay = _target_haystack(record)
    if target_type in ("nif", "cif"):
        return target.upper() in hay.upper()
    return target.lower() in hay.lower()


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def fetch_contratacion(
    bundles: list[tuple[int, int]],
    target: str,
    target_type: str,
    count: int,
) -> tuple[list[dict[str, Any]], dict[str, Any], str | None]:
    """Download + parse + filter, returning (records, meta, error)."""
    if not REQUESTS_AVAILABLE:
        return [], {}, "requests library not available"

    results: list[dict[str, Any]] = []
    meta: dict[str, Any] = {
        "bundles_requested": [f"{y}{m:02d}" for (y, m) in bundles],
        "bundles_processed": [],
        "bundles_failed": [],
        "entries_seen": 0,
        "entries_matched": 0,
        "truncated": False,
    }
    truncated = False
    seen_ids: set[str] = set()

    for (year, month) in bundles:
        try:
            blob = _download_zip(year, month)
        except Exception as exc:
            logger.warning(
                "bundle download failed",
                extra_data={"year": year, "month": month, "error": str(exc)},
            )
            meta["bundles_failed"].append({"year": year, "month": month, "error": str(exc)})
            continue
        try:
            bodies = _zip_bodies(blob)
        except Exception as exc:
            logger.warning(
                "zip extraction failed",
                extra_data={"year": year, "month": month, "error": str(exc)},
            )
            meta["bundles_failed"].append({"year": year, "month": month, "error": str(exc)})
            continue
        meta["bundles_processed"].append(f"{year}{month:02d}")

        for fname, body in bodies:
            if len(results) >= count:
                truncated = True
                break
            for record in _iter_entries(body):
                meta["entries_seen"] += 1
                rid = record.get("id") or ""
                if rid and rid in seen_ids:
                    continue
                if rid:
                    seen_ids.add(rid)
                if not _matches_target(record, target, target_type):
                    continue
                results.append(record)
                meta["entries_matched"] += 1
                if len(results) >= count:
                    truncated = True
                    break
            if len(results) >= count:
                truncated = True
                break
            if meta["entries_seen"] >= MAX_TOTAL_ENTRIES:
                truncated = True
                break
        if truncated:
            break

    meta["truncated"] = truncated
    return results, meta, None


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

def _format_amount(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.2f} EUR"


def _format_record_line(idx: int, record: dict[str, Any]) -> str:
    buyer = record.get("buyer") or {}
    return (
        f"**{idx}. {record.get('title', '')}**\n"
        f"Id: {record.get('folder_id', '')} · Estado: {record.get('status', '')} · "
        f"Procedimiento: {record.get('procedure_code', '')}\n"
        f"Órgano: {buyer.get('name', '')} (NIF {buyer.get('nif', '—')})\n"
        f"Importe estimado: {_format_amount(record.get('estimated_amount'))} · "
        f"Total: {_format_amount(record.get('total_amount'))}\n"
        f"URL: {record.get('url', '')}"
    )


def write_response(data: dict[str, Any]) -> None:
    print(json.dumps(data, default=str, ensure_ascii=False), flush=True)


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

        try:
            bundles = _resolve_bundles(args)
        except ValueError as exc:
            write_response({
                "success": False,
                "request_id": request_id,
                "error": {"code": "INVALID_INPUT", "message": str(exc)},
            })
            return

        if len(bundles) > 24:
            write_response({
                "success": False,
                "request_id": request_id,
                "error": {
                    "code": "RANGE_TOO_LARGE",
                    "message": (
                        f"requested {len(bundles)} monthly bundles, max 24 per call"
                    ),
                },
            })
            return

        target = str(args.get("target", "")).strip()
        target_type = str(args.get("target_type", "nif")).lower()
        if target_type not in ("nif", "cif", "name"):
            target_type = "nif"
        try:
            count = int(args.get("count", 50))
        except (TypeError, ValueError):
            count = 50
        count = max(1, min(count, 200))

        results, meta, error = fetch_contratacion(bundles, target, target_type, count)
        if error:
            write_response({
                "success": False,
                "request_id": request_id,
                "error": {"code": "FETCH_FAILED", "message": error},
            })
            return

        bundles_label = ", ".join(meta["bundles_processed"]) or ", ".join(
            meta["bundles_requested"]
        )
        target_label = target if target else "(sin filtro)"
        header = f"**Contratación del Sector Público — {bundles_label}**"
        subheader = f"Objetivo: {target_label} · Coincidencias: {meta['entries_matched']}"
        if meta.get("truncated"):
            subheader += " (truncado)"

        lines = [header, subheader, ""]
        if not results:
            if meta["bundles_failed"]:
                fails = "; ".join(
                    f"{f['year']}{f['month']:02d}: {f['error']}"
                    for f in meta["bundles_failed"]
                )
                lines.append(f"**Aviso:** no se pudieron descargar: {fails}")
            else:
                lines.append("No se encontraron resultados.")
        else:
            for i, r in enumerate(results, 1):
                lines.append(_format_record_line(i, r))
                lines.append("")

        write_response({
            "success": True,
            "request_id": request_id,
            "content": [{"type": "text", "text": "\n".join(lines).rstrip()}],
            "structured_content": {
                "source": "contratacion",
                "bundles": meta["bundles_requested"],
                "bundles_processed": meta["bundles_processed"],
                "bundles_failed": meta["bundles_failed"],
                "target": target,
                "target_type": target_type,
                "results": results,
                "count": len(results),
                "entries_seen": meta["entries_seen"],
                "entries_matched": meta["entries_matched"],
                "truncated": meta["truncated"],
            },
        })
    except Exception as exc:
        logger.error(
            "unhandled exception in contratacion_fetch",
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
