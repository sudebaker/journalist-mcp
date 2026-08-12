#!/usr/bin/env python3
"""transparency_fetch — search the EU Transparency Register bulk XML.

Downloads the official bulk XML export from the European Commission's
Transparency Register, caches the raw + a scrubbed (XML 1.0-clean) copy
in the working directory, streams the cleaned copy with iterparse
(no full DOM load), and filters by query / category / country. Output is
a SubprocessResponse JSON on STDOUT, matching the journalist-mcp tool
protocol.

The bulk export is ~110MB of XML describing 17,000+ registered interest
representatives (lobbyists, NGOs, trade associations, etc.).

API: https://transparency-register.europa.eu/odplastorganisationxml_en
"""
import gzip
import json
import os
import re
import sys
import tempfile
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common.structured_logging import get_logger

logger = get_logger(__name__, "transparency_fetch")

from common.http import request_with_retry, REQUESTS_AVAILABLE

TRANSPARENCY_URL = "https://transparency-register.europa.eu/odplastorganisationxml_en"
# Files in working_dir (we get the working dir from SubprocessContext).
CACHE_RAW = "transparency_register.xml"
CACHE_CLEAN = "transparency_register.cleaned.xml"

# Threshold (seconds) after which the cached bulk is considered stale and
# will be re-downloaded on the next request. The EU source itself is
# cache-control: max-age=300, so anything older than ~12h is a safe refresh.
CACHE_MAX_AGE_SECONDS = 12 * 3600

MAX_RESULTS = 200
DEFAULT_LIMIT = 20

# XML 1.0 valid char ranges. Anything outside is replaced with a space so
# the bulk export's handful of bad bytes (em-dashes encoded oddly, etc.)
# don't poison the whole iterparse stream.
_INVALID_XML_CHAR = re.compile(
    r"[^\x09\x0A\x0D\x20-\uD7FF\uE000-\uFFFD\U00010000-\U0010FFFF]"
)

# XML 1.0 disallows numeric character references to certain code points:
# 0x00-0x08, 0x0B-0x0C, 0x0E-0x1F, 0xD800-0xDFFF, 0xFFFE-0xFFFF and
# any code point above 0x10FFFF. We match decimal and hex forms and
# replace any whose value falls outside the legal range with a space.
_DECIMAL_REF = re.compile(r"&#([0-9]+);")
_HEX_REF = re.compile(r"&#x([0-9A-Fa-f]+);")


def _is_valid_xml_codepoint(cp: int) -> bool:
    if cp < 0x20:
        return cp in (0x09, 0x0A, 0x0D)
    if 0xD800 <= cp <= 0xDFFF:
        return False
    if 0xFFFE <= cp <= 0xFFFF:
        return False
    if cp > 0x10FFFF:
        return False
    return True


def _scrub_invalid_char_refs(text: str) -> str:
    """Replace &#N; / &#xN; references whose codepoint is illegal in XML 1.0."""
    def _sub_dec(m):
        try:
            cp = int(m.group(1))
        except ValueError:
            return m.group(0)
        return m.group(0) if _is_valid_xml_codepoint(cp) else " "

    def _sub_hex(m):
        try:
            cp = int(m.group(1), 16)
        except ValueError:
            return m.group(0)
        return m.group(0) if _is_valid_xml_codepoint(cp) else " "

    text = _DECIMAL_REF.sub(_sub_dec, text)
    text = _HEX_REF.sub(_sub_hex, text)
    return text


def _clean_text(s: str | None) -> str:
    """Normalize whitespace and strip invalid XML chars from a string."""
    if not s:
        return ""
    cleaned = _INVALID_XML_CHAR.sub(" ", s)
    return re.sub(r"\s+", " ", cleaned).strip()


def _workdir_from_request(request: dict[str, Any]) -> str:
    """Resolve the working directory from SubprocessContext, env, or system temp.

    Falls back to ``tempfile.gettempdir()`` (e.g. ``/tmp`` on Linux) so the
    tool works in environments where neither the executor's
    ``context.working_dir`` nor a writable ``/data`` directory exist. A
    dedicated ``TRANSPARENCY_CACHE_DIR`` still takes precedence over the
    system temp dir when the operator wants long-lived caching.
    """
    ctx = request.get("context") or {}
    wd = (ctx.get("working_dir")
          or os.environ.get("TRANSPARENCY_CACHE_DIR")
          or tempfile.gettempdir())
    return wd


def _is_cache_fresh(path: str) -> bool:
    if not os.path.exists(path):
        return False
    try:
        age = os.path.getmtime(path)
    except OSError:
        return False
    import time
    return (time.time() - age) < CACHE_MAX_AGE_SECONDS


def _download_bulk(dest_path: str, timeout: int) -> tuple[bool, str | None]:
    """Download the bulk XML to dest_path. Returns (ok, error)."""
    if not REQUESTS_AVAILABLE:
        return False, "requests library not available"
    try:
        resp = request_with_retry(
            "GET", TRANSPARENCY_URL,
            stream=True,
            timeout=timeout,
            headers={"Accept": "application/xml, */*"},
        )
        try:
            if resp.status_code != 200:
                return False, f"HTTP {resp.status_code} from transparency register"
            tmp = dest_path + ".part"
            with open(tmp, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=1 << 20):
                    if chunk:
                        fh.write(chunk)
            os.replace(tmp, dest_path)
        finally:
            resp.close()
        return True, None
    except Exception as e:
        msg = str(e).lower()
        if "timeout" in msg or "timed" in msg:
            return False, "transparency register download timed out"
        return False, f"download failed: {type(e).__name__}: {e}"


def _ensure_raw_cache(workdir: str, force_refresh: bool) -> tuple[str, str | None]:
    """Make sure the raw bulk XML file exists in workdir.

    Returns (path, error). Path is the file to read; error is None on success.
    """
    raw_path = os.path.join(workdir, CACHE_RAW)

    if not force_refresh and _is_cache_fresh(raw_path):
        return raw_path, None

    ok, err = _download_bulk(raw_path, timeout=180)
    if not ok and os.path.exists(raw_path):
        # Re-download failed (e.g. transient network) but we still have a
        # previous copy. Use it; the cleaner will still produce a valid
        # file even if a bit stale.
        logger.warning("Re-download failed; using existing raw cache",
                       extra_data={"error": err})
        return raw_path, None
    if not ok:
        return "", err or "download failed and no cached file available"
    return raw_path, None


def _build_clean_cache(src_path: str, dest_path: str) -> str | None:
    """Stream src_path (raw or gz) into dest_path with invalid XML chars
    replaced. Returns None on success, error string on failure.
    """
    try:
        if src_path.endswith(".gz"):
            src_fh_obj = gzip.open(src_path, "rb")
        else:
            src_fh_obj = open(src_path, "rb")

        def _chunks(fh):
            while True:
                b = fh.read(1 << 20)
                if not b:
                    break
                yield b

        os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
        with src_fh_obj, open(dest_path + ".part", "wb") as out:
            for chunk in _chunks(src_fh_obj):
                # Decode loosely, scrub invalid literal bytes AND numeric
                # character references (e.g. &#x2;) that XML 1.0 forbids,
                # then re-encode.
                text = chunk.decode("utf-8", errors="replace")
                text = _INVALID_XML_CHAR.sub(" ", text)
                text = _scrub_invalid_char_refs(text)
                out.write(text.encode("utf-8"))
        os.replace(dest_path + ".part", dest_path)
        return None
    except Exception as e:
        return f"cleaning failed: {type(e).__name__}: {e}"


def _ensure_clean_cache(workdir: str, raw_path: str) -> tuple[str, str | None]:
    """Make sure the cleaned XML is on disk; build it from raw if needed.

    The cleaned cache is keyed by the raw file's mtime so a fresh download
    triggers a fresh cleaning pass.
    """
    clean_path = os.path.join(workdir, CACHE_CLEAN)
    raw_mtime = os.path.getmtime(raw_path)
    if os.path.exists(clean_path):
        try:
            if os.path.getmtime(clean_path) >= raw_mtime - 1:
                return clean_path, None
        except OSError:
            pass
    err = _build_clean_cache(raw_path, clean_path)
    if err:
        # Fall back to streaming from raw directly (parse errors mid-stream
        # will be caught by the caller).
        if os.path.exists(clean_path):
            os.remove(clean_path)
        return raw_path, None
    return clean_path, None


def _local_name(tag: str) -> str:
    """Strip XML namespace from a tag."""
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _text(node, path: list[str]) -> str:
    """Read a nested text value, returning '' on miss. Path is a list of
    local child names.
    """
    cur = node
    for step in path:
        if cur is None:
            return ""
        cur = cur.find(step)
    if cur is None or cur.text is None:
        return ""
    return _clean_text(cur.text)


def _parse_stream(xml_path: str):
    """Stream <interestRepresentative> records from xml_path as plain dicts.

    We use a custom parser that recovers from bad bytes by replacing them
    in 1MB blocks before feeding to expat. This handles the occasional
    invalid character in the EU bulk without losing the whole stream.
    """
    # Re-clean on-the-fly via a temp file so iterparse gets valid XML.
    tmp = tempfile.NamedTemporaryFile(prefix="transparency_", suffix=".xml",
                                      delete=False)
    tmp_path = tmp.name
    tmp.close()
    try:
        with open(xml_path, "rb") as src, open(tmp_path, "wb") as out:
            while True:
                chunk = src.read(1 << 20)
                if not chunk:
                    break
                text = chunk.decode("utf-8", errors="replace")
                text = _INVALID_XML_CHAR.sub(" ", text)
                text = _scrub_invalid_char_refs(text)
                out.write(text.encode("utf-8"))
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise

    import xml.etree.ElementTree as ET
    try:
        for event, elem in ET.iterparse(tmp_path, events=("end",)):
            if _local_name(elem.tag) == "interestRepresentative":
                rec = {
                    "identification_code": _text(elem, ["identificationCode"]),
                    "name": _text(elem, ["name", "originalName"]),
                    "acronym": _text(elem, ["acronym"]),
                    "entity_form": _text(elem, ["entityForm"]),
                    "website": _text(elem, ["webSiteURL"]),
                    "registration_category": _text(elem, ["registrationCategory"]),
                    "registration_date": _text(elem, ["registrationDate"]),
                    "last_update_date": _text(elem, ["lastUpdateDate"]),
                    "country": _text(elem, ["headOffice", "country"]),
                    "city": _text(elem, ["headOffice", "city"]),
                    "ep_accreditation": _text(elem, ["EPAccreditedNumber"]),
                }
                yield rec
                # Free the subtree so iterparse stays memory-light.
                elem.clear()
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def _matches(rec: dict[str, Any], query: str, category: str,
             country: str) -> bool:
    q = (query or "").lower()
    if q and q not in (rec.get("name", "") or "").lower() \
            and q not in (rec.get("acronym", "") or "").lower():
        return False
    if category and category.lower() not in (rec.get("registration_category", "")
                                              or "").lower():
        return False
    if country and country.lower() not in (rec.get("country", "") or "").lower():
        return False
    return True


def search_transparency(query: str, category: str, country: str,
                        limit: int, refresh: bool, workdir: str
                        ) -> tuple[list[dict[str, Any]], dict[str, Any], str | None]:
    """Search the cached bulk. Returns (results, stats, error)."""
    stats: dict[str, Any] = {
        "scanned": 0,
        "matched": 0,
        "truncated": False,
        "cache_path": None,
        "cleaned": False,
    }
    if not REQUESTS_AVAILABLE:
        return [], stats, "requests library not available"
    raw_path, err = _ensure_raw_cache(workdir, refresh)
    if err:
        return [], stats, err
    clean_path, _ = _ensure_clean_cache(workdir, raw_path)
    stats["cleaned"] = (clean_path != raw_path)
    stats["cache_path"] = clean_path

    results: list[dict[str, Any]] = []
    try:
        for rec in _parse_stream(clean_path):
            stats["scanned"] += 1
            if _matches(rec, query, category, country):
                results.append(rec)
                stats["matched"] += 1
                if len(results) >= limit:
                    stats["truncated"] = True
                    break
    except Exception as e:
        return results, stats, f"parse failed: {type(e).__name__}: {e}"
    return results, stats, None


def _format_markdown(query: str, category: str, country: str,
                     results: list[dict[str, Any]],
                     stats: dict[str, Any]) -> str:
    title_bits = []
    if query:
        title_bits.append(f"«{query}»")
    if category:
        title_bits.append(f"categoría: {category}")
    if country:
        title_bits.append(f"país: {country}")
    header = "**EU Transparency Register"
    if title_bits:
        header += " — " + " · ".join(title_bits)
    header += "**\n"
    lines = [header]
    if not results:
        lines.append("Sin resultados en el registro.")
    else:
        for i, r in enumerate(results, 1):
            name = r.get("name") or r.get("acronym") or "(sin nombre)"
            cat = r.get("registration_category") or "(sin categoría)"
            loc = ", ".join(filter(None, [r.get("city"), r.get("country")]))
            loc_s = f" — {loc}" if loc else ""
            lines.append(f"**{i}. {name}**")
            lines.append(f"Categoría: {cat}{loc_s}")
            if r.get("acronym"):
                lines.append(f"Acrónimo: {r['acronym']}")
            if r.get("website"):
                lines.append(f"Web: {r['website']}")
            if r.get("identification_code"):
                lines.append(f"ID: {r['identification_code']}")
            lines.append("")
    lines.append("---")
    trunc = " (truncado)" if stats.get("truncated") else ""
    lines.append(
        f"Escaneados: {stats.get('scanned', 0)} · "
        f"Coincidencias: {stats.get('matched', 0)}{trunc} · "
        f"Cache: `{os.path.basename(stats.get('cache_path') or '')}`"
    )
    return "\n".join(lines)


def write_response(data: dict[str, Any]) -> None:
    print(json.dumps(data, default=str, ensure_ascii=False), flush=True)


def main() -> None:
    request: dict = {}
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            write_response({
                "success": False, "request_id": "",
                "error": {"code": "EMPTY_STDIN",
                          "message": "no JSON request received on stdin"},
            })
            return
        request = json.loads(raw)
        request_id = request.get("request_id", "")
        args = request.get("arguments", {}) or {}

        query = str(args.get("query", "")).strip()
        category = str(args.get("category", "")).strip()
        country = str(args.get("country", "")).strip()

        try:
            limit = int(args.get("limit", DEFAULT_LIMIT))
        except (TypeError, ValueError):
            limit = DEFAULT_LIMIT
        limit = max(1, min(limit, MAX_RESULTS))

        refresh = bool(args.get("refresh", False))

        workdir = _workdir_from_request(request)
        if not os.path.isdir(workdir):
            try:
                os.makedirs(workdir, exist_ok=True)
            except OSError as e:
                write_response({
                    "success": False, "request_id": request_id,
                    "error": {
                        "code": "WORKDIR_UNAVAILABLE",
                        "message": f"cannot use working dir '{workdir}': {e}",
                    },
                })
                return

        if not query and not category and not country:
            # Allow a bare listing, but require a limit small enough that
            # we don't accidentally dump 17k rows. The default (20) is fine.
            pass

        logger.info(
            "transparency_fetch start",
            extra_data={"query": query, "category": category,
                        "country": country, "limit": limit,
                        "refresh": refresh, "workdir": workdir},
        )

        results, stats, error = search_transparency(
            query, category, country, limit, refresh, workdir,
        )

        if error and not results:
            write_response({
                "success": False, "request_id": request_id,
                "error": {"code": "FETCH_FAILED", "message": error,
                          "details": stats},
            })
            return

        md = _format_markdown(query, category, country, results, stats)
        write_response({
            "success": True, "request_id": request_id,
            "content": [{"type": "text", "text": md}],
            "structured_content": {
                "source": "eu_transparency_register",
                "query": query,
                "category": category,
                "country": country,
                "limit": limit,
                "results": results,
                "count": len(results),
                "stats": stats,
            },
        })
    except json.JSONDecodeError:
        write_response({
            "success": False, "request_id": "",
            "error": {"code": "INVALID_JSON", "message": "Failed to parse JSON"},
        })
    except Exception as e:
        logger.error("Unhandled exception", extra_data={"error": str(e)})
        write_response({
            "success": False,
            "request_id": request.get("request_id", "") if isinstance(request, dict) else "",
            "error": {"code": "EXECUTION_FAILED", "message": str(e)},
        })


if __name__ == "__main__":
    main()
