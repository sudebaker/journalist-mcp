# OSINT-ES Sources — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the `osint-es` toolset in `journalist-mcp` by finishing the `bdns_search` and `contratacion_search` stubs and adding two new sources (`doue_search` for EUR-Lex, `transparency_search` for the Spanish Transparency Portal), plus shared infrastructure helpers used by all tools.

**Architecture:** Each tool is a stateless Python subprocess discovered via `tool.yaml` manifest, communicating over JSON stdin/stdout. An existing Go orchestrator (`journalist_investigate`) fans these out in parallel with `errgroup`. The change adds 3 shared helpers (`tools/common/{evidence,http,entity_normalizer}.py`), completes/creates 4 Python tools, migrates 4 existing tools to the shared HTTP helper, fixes 1 Go orchestration file, and re-registers 2 tools in `configs/toolsets.yaml`.

**Tech Stack:** Python 3 (`requests` + stdlib: `xml.etree`, `html.parser`, `zipfile`, `tempfile`, `hashlib`), Go 1.23 (orchestrator, config discovery), YAML manifests. No new external dependencies beyond `requests` which is already used.

## Global Constraints

- Go 1.23.0 (module pinned; Go version corrected in Dockerfile:1.23).
- Python tools import only `requests` (already in repo); all XML/HTML/ZIP/hashing via stdlib.
- Response contract: stdout = single JSON object; stderr = structured logs via `tools/common/structured_logging.get_logger`. Every tool MUST use `sys.path.insert(0, "../")` + `from common.structured_logging import get_logger`.
- `tool.yaml` discovery is automatic from `tools/` subdirs containing `tool.yaml`; no Go registration needed for Python tools.
- `internal/orchestrator/investigate.go:44-46` `defaultSources` MUST be changed from hardcoded to dynamic.
- All human-readable `content` text is Spanish (matches existing tools: "No se encontraron resultados."). Structured/JSON fields are English.

---

### Task 1: Shared HTTP helper (`tools/common/http.py`)

**Files:**
- Create: `tools/common/http.py`
- Depend on: `tools/common/structured_logging.py` (exists)

**Interfaces:**
- Consumes: `get_logger` from `structured_logging`.
- Produces: `get_session() → requests.Session`, `request_with_retry(method, url, params=None, json=None, headers=None, timeout=30, max_retries=3) → requests.Response`.

- [ ] **Step 1: Write the failing test**

Create `tests/common/test_http.py`:
```python
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))
from common.http import request_with_retry

def test_request_with_retry_success():
    # uses httpbin via local stub? No -- mock. But no test infra exists.
    # Smoke: call a known-good endpoint (BOE datosabiertos) and assert 200.
    resp = request_with_retry("GET", "https://www.boe.es/datosabiertos/api/boe/sumario/20240101", timeout=15)
    assert resp is not None
```
Run: `python3 tests/common/test_http.py`  (manual smoke, since no pytest infra)

- [ ] **Step 2: Run test to verify it fails**
Expected: ImportError / module not found.

- [ ] **Step 3: Minimal implementation**

```python
#!/usr/bin/env python3
"""Shared HTTP session with retry/backoff and 429/503 Retry-After handling."""
import time
from typing import Optional
import requests
from requests.adapters import HTTPAdapter
try:
    from urllib3.util.retry import Retry
except ImportError:
    Retry = None
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common.structured_logging import get_logger

logger = get_logger(__name__, "common_http")

_USER_AGENT = "journalist-mcp/1.0 (+https://github.com/sudebaker/journalist-mcp)"

def get_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": _USER_AGENT, "Accept": "application/json"})
    return session

def request_with_retry(method, url, params=None, json=None, headers=None,
                      timeout=30, max_retries=3) -> requests.Response:
    if Retry is None:
        logger.warning("urllib3 Retry not available, falling back to basic")
        session = get_session()
        kwargs = {"timeout": timeout}
        if params: kwargs["params"] = params
        if json: kwargs["json"] = json
        if headers: kwargs["headers"] = headers
        return session.request(method, url, **kwargs)
    retry = Retry(total=max_retries, backoff_factor=0.5, allowed_methods=["GET","POST"],
                  status_forcelist=[429, 500, 502, 503, 504],
                  respect_retry_after_header=True, raise_on_status=False)
    adapter = HTTPAdapter(max_retries=retry)
    session = get_session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    final_headers = headers or {}
    logger.info("http_request_start", extra_data={"method": method, "url": url})
    resp = session.request(method, url, params=params, json=json,
                           headers={**session.headers, **final_headers}, timeout=timeout)
    logger.info("http_request_end", extra_data={"status": resp.status_code, "url": url})
    return resp
```

- [ ] **Step 4: Run test to verify it passes**
Run: `python3 tests/common/test_http.py`
Expected: PASS (HTTP 200 from BOE datosabiertos endpoint).

- [ ] **Step 5: Commit**
```bash
mkdir -p tests/common
touch tests/common/__init__.py
git add tools/common/http.py tests/common/test_http.py
git commit -m "feat: add shared HTTP helper with retry/backoff"
```

---

### Task 2: Shared Evidence helper (`tools/common/evidence.py`)

**Files:**
- Create: `tools/common/evidence.py`
- Test: `tests/common/test_evidence.py`

**Interfaces:**
- Consumes: stdlib `hashlib`.
- Produces: `build_evidence(source, official, confidence, title, date, url, metadata, retrieved_at=None, query=None) → dict`.

- [ ] **Step 1: Write the failing test**

```python
import sys, os, hashlib
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))
from common.evidence import build_evidence

def test_build_evidence_structure():
    ev = build_evidence("BDNS", True, 1.0, "Subvención X", "2025-06-15",
                        "https://ejemplo.es", {"nif": "A123"}, "test-query")
    assert ev["source"] == "BDNS"
    assert ev["official"] is True
    assert ev["confidence"] == 1.0
    assert ev["title"] == "Subvención X"
    assert ev["date"] == "2025-06-15"
    assert ev["url"] == "https://ejemplo.es"
    assert ev["metadata"]["nif"] == "A123"
    assert "id" in ev and len(ev["id"]) == 16
    assert ev["retrieved_at"] != ""

def test_build_evidence_id_is_deterministic():
    ev1 = build_evidence("BDNS", True, 1.0, "Same", "2025-01-01", "https://x.es", {})
    ev2 = build_evidence("BDNS", True, 1.0, "Same", "2025-01-01", "https://x.es", {})
    assert ev1["id"] == ev2["id"]

def test_build_evidence_id_differs_on_url():
    ev1 = build_evidence("BDNS", True, 1.0, "Same", "2025-01-01", "https://a.es", {})
    ev2 = build_evidence("BDNS", True, 1.0, "Same", "2025-01-01", "https://b.es", {})
    assert ev1["id"] != ev2["id"]
```
Run: `python3 tests/common/test_evidence.py`

- [ ] **Step 2: Run test to verify it fails**
Expected: ImportError.

- [ ] **Step 3: Minimal implementation**

```python
#!/usr/bin/env python3
"""Common Evidence model for all OSINT tools."""
import hashlib
from datetime import datetime, timezone
from typing import Any
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common.structured_logging import get_logger
logger = get_logger(__name__, "common_evidence")

def build_evidence(source: str, official: bool, confidence: float,
                   title: str, date: str, url: str, metadata: dict[str, Any],
                   query: str = "") -> dict:
    raw = f"{url}|{date}|{title}"
    ev_id = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return {
        "id": ev_id,
        "source": source,
        "official": official,
        "confidence": confidence,
        "title": title,
        "date": date,
        "url": url,
        "metadata": metadata,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "query": query,
    }
```

- [ ] **Step 4: Run test to verify it passes**
Run: `python3 tests/common/test_evidence.py`
Expected: 3/3 PASS.

- [ ] **Step 5: Commit**
```bash
git add tools/common/evidence.py tests/common/test_evidence.py
git commit -m "feat: add shared Evidence model helper"
```

---

### Task 3: Shared Entity Normalizer (`tools/common/entity_normalizer.py`)

**Files:**
- Create: `tools/common/entity_normalizer.py`
- Test: `tests/common/test_entity_normalizer.py`

**Interfaces:**
- Consumes: stdlib `unicodedata`, `re`.
- Produces: `normalize_for_match(name: str) → str`.

- [ ] **Step 1: Write the failing test**

```python
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))
from common.entity_normalizer import normalize_for_match

def test_normalizes_accents():
    assert normalize_for_match("Bancó Santándér") == normalize_for_match("Banco Santander")

def test_strips_legal_forms():
    assert "sa" not in normalize_for_match("Banco Santander, S.A.")
    assert normalize_for_match("Empresa S.L.") == normalize_for_match("empresa")
    assert normalize_for_match("Empresa S.L.U.") == normalize_for_match("empresa")

def test_lowercase_and_collapse():
    assert normalize_for_match("  BANCO   SANTANDER  ") == "banco santander"

def test_none_input():
    assert normalize_for_match(None) == ""
```
Run: `python3 tests/common/test_entity_normalizer.py`

- [ ] **Step 2: Run test to verify it fails**
Expected: ImportError.

- [ ] **Step 3: Minimal implementation**

```python
#!/usr/bin/env python3
"""Entity normalization for cross-tool matching (not display)."""
import unicodedata
import re
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common.structured_logging import get_logger
logger = get_logger(__name__, "common_entity_normalizer")

_LEGAL_FORMS = re.compile(r"\b(s\.?a\.?|s\.?l\.?|s\.?l\.?u\.?|sociedad\s+anonima|sociedad\s+limitada)\b",
                          re.IGNORECASE)

def normalize_for_match(name: str | None) -> str:
    if not name:
        return ""
    nfkd = unicodedata.normalize("NFKD", name)
    no_accents = "".join(c for c in nfkd if not unicodedata.combining(c))
    no_legal = _LEGAL_FORMS.sub(" ", no_accents)
    no_punct = re.sub(r"[,\.\-\n\r]", " ", no_legal)
    collapsed = re.sub(r"\s+", " ", no_punct).strip()
    return collapsed.lower()
```

- [ ] **Step 4: Run test to verify it passes**
Run: `python3 tests/common/test_entity_normalizer.py`
Expected: 4/4 PASS.

- [ ] **Step 5: Commit**
```bash
git add tools/common/entity_normalizer.py tests/common/test_entity_normalizer.py
git commit -m "feat: add shared entity normalizer for matching"
```

---

### Task 4: Complete `bdns_search` (stub → API JSON)

**Files:**
- Modify: `tools/bdns_search/tool.yaml`
- Rewrite: `tools/bdns_search/main.py`

**Interfaces:**
- Consumes: `common.http.request_with_retry`, `common.evidence.build_evidence`, `common.entity_normalizer.normalize_for_match`, `common.structured_logging.get_logger`.
- Produces: stdout JSON `{"success": true, "content": [...], "structured_content": {"source":"BDNS", "target":..., "official":true, "confidence":1.0, "results":[Evidence...], "count":N}}`.

- [ ] **Step 1: Update `tool.yaml`** (add date_from, date_to, count)
```yaml
name: bdns_search
description: "Busca subvenciones en BDNS (Base de Datos Nacional de Subvenciones) por beneficiario (NIF/CIF/nombre). API REST JSON oficial."
command: python3
args:
  - main.py
timeout: 120s
input_schema:
  type: object
  properties:
    target:
      type: string
      description: "NIF/CIF o nombre del beneficiario"
    target_type:
      type: string
      enum: [nif, cif, name]
      default: nif
      description: "Tipo de identificador"
    date_from:
      type: string
      description: "Filtrar desde (YYYY-MM-DD). Default: 1 año atrás."
    date_to:
      type: string
      description: "Filtrar hasta (YYYY-MM-DD). Default: hoy."
    count:
      type: integer
      default: 20
      description: "Máximo resultados (default 20)"
  required:
    - target
```

- [ ] **Step 2: Write the new `main.py`**

```python
#!/usr/bin/env python3
import json, os, sys
from datetime import datetime, timedelta
from typing import Any
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common.structured_logging import get_logger
from common.http import request_with_retry
from common.evidence import build_evidence

logger = get_logger(__name__, "bdns_search")
BDNS_API = "https://www.pap.hacienda.gob.es/bdnstrans/api/consulta-beneficiarios"
SOURCE = "BDNS"
DEFAULT_LOOKBACK_DAYS = 365

def _parse_date(s, default):
    try:
        return datetime.strptime(s, "%Y-%m-%d")
    except (ValueError, TypeError):
        return default

def _format_date(d):
    return d.strftime("%Y%m%d")

def search_bdns(target: str, target_type: str, date_from: str, date_to: str, count: int):
    desde = _parse_date(date_from, datetime.now() - timedelta(days=DEFAULT_LOOKBACK_DAYS))
    hasta = _parse_date(date_to, datetime.now())
    # NOTE: BDNS API params inferred from endpoint; names verified against bdnstrans JS.
    # If schema changes, API returns 400 and we log the raw response.
    params = {
        "fechaDesdeConcesion": _format_date(desde),
        "fechaHastaConcesion": _format_date(hasta),
        "page": 0,
        "pageSize": min(count, 200),
    }
    if target_type in ("nif", "cif"):
        params["nifBeneficiario"] = target
    else:
        params["nombreBeneficiario"] = target
    results = []
    while len(results) < count:
        resp = request_with_retry("GET", BDNS_API, params=params, timeout=30)
        if resp.status_code != 200:
            logger.error("bdns_api_failed", extra_data={"status": resp.status_code, "url": resp.url})
            return None, f"BDNS API HTTP {resp.status_code}"
        try:
            data = resp.json()
        except ValueError:
            return None, "BDNS API response not JSON"
        items = data.get("resultados", []) if isinstance(data, dict) else data
        if isinstance(items, dict):
            items = items.get("rows", [items]) if "rows" in items else [items]
        if not items:
            break
        for item in items:
            if isinstance(item, dict):
                results.append(item)
                if len(results) >= count:
                    break
        params["page"] += 1
        if len(items) < params["pageSize"]:
            break
    evidences = [_to_evidence(r, target) for r in results]
    return evidences, None

def _to_evidence(r, query):
    return build_evidence(
        source=SOURCE,
        official=True,
        confidence=1.0,
        title=r.get("titulo", r.get("objetivo", "Sin título")),
        date=r.get("fechaConcesion", r.get("fechaPublicacion", "")),
        url=r.get("urlDetalle", r.get("enlace", "")),
        metadata={
            "nif": r.get("nifBeneficiario", ""),
            "beneficiario": r.get("nombreBeneficiario", ""),
            "organo": r.get("organo", r.get("departamento", "")),
            "importe": r.get("importe", ""),
            "finalidad": r.get("finalidad", ""),
        },
        query=query,
    )

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
        target_type = args.get("target_type", "nif")
        count = int(args.get("count", 20))
        date_from = args.get("date_from", "")
        date_to = args.get("date_to", "")
        results, error = search_bdns(target, target_type, date_from, date_to, count)
        if error:
            write_response({"success": False, "request_id": request_id,
                            "error": {"code": "SEARCH_FAILED", "message": error}})
            return
        lines = [f"**BDNS — Resultados para {target}**\n"]
        if not results:
            lines.append("No se encontraron resultados.")
        for i, r in enumerate(results, 1):
            lines.append(f"**{i}. {r.get('title','')}**")
            lines.append(f"- Fecha: {r.get('date','')}")
            lines.append(f"- Beneficiario: {r.get('metadata',{}).get('beneficiario','')}")
            lines.append(f- Importe: {r.get('metadata',{}).get('importe','')})")
            lines.append(f"- Más info: {r.get('url','')}")
            lines.append("")
        write_response({
            "success": True,
            "request_id": request_id,
            "content": [{"type": "text", "text": "\n".join(lines)}],
            "structured_content": {
                "source": SOURCE, "target": target, "official": True,
                "confidence": 1.0, "results": results, "count": len(results),
                "retrieved_at": datetime.now(timezone.utc).isoformat(),
            },
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
```

- [ ] **Step 3: Run smoke test**
```bash
echo '{"request_id":"t","arguments":{"target":"Banco Santander","target_type":"name","date_from":"2025-01-01","date_to":"2026-01-01"}}' | python3 tools/bdns_search/main.py
```
Expected: `{"success":true,...}` con `results` no vacío (o count:0 si realmente no hay subvenciones a ese nombre).

- [ ] **Step 4: Fix any errors from smoke test output**

- [ ] **Step 5: Commit**
```bash
git add tools/bdns_search/main.py tools/bdns_search/tool.yaml
git commit -m "feat: complete bdns_search with BDNS API JSON integration"
```

---

### Task 5: Complete `contratacion_search` (stub → ATOM feed híbrido)

**Files:**
- Modify: `tools/contratacion_search/tool.yaml`
- Rewrite: `tools/contratacion_search/main.py`

**Interfaces:**
- Consumes: same shared helpers + stdlib `xml.etree`, `zipfile`, `tempfile`, `shutil`.
- Produces: `structured_content` con `results:[Evidence...]` y `count`.

- [ ] **Step 1: Update `tool.yaml`**
```yaml
name: contratacion_search
description: "Busca licitaciones y contratos en Contratación del Estado por NIF/CIF/nombre de adjudicatario. Feed ATOM CODICE + archivos .zip anuales."
command: python3
args: [main.py]
timeout: 120s
input_schema:
  type: object
  properties:
    target: {type: string}
    target_type: {type: string, enum: [nif, cif, name], default: nif}
    date_from: {type: string, description: "YYYY-MM-DD. Default: 30 días atrás."}
    date_to: {type: string, description: "YYYY-MM-DD. Default: hoy."}
    count: {type: integer, default: 20}
  required: [target]
```

- [ ] **Step 2: Write the new `main.py`**

```python
#!/usr/bin/env python3
import json, os, sys, io, zipfile, tempfile, shutil, re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from typing import Any, Optional
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common.structured_logging import get_logger
from common.http import request_with_retry
from common.evidence import build_evidence
from common.entity_normalizer import normalize_for_match

logger = get_logger(__name__, "contratacion_search")
LIVE_FEED = "https://contrataciondelsectorpublico.gob.es/sindicacion/sindicacion_643/licitacionesPerfilesContratanteCompleto3.atom"
ZIP_BASE = "https://contrataciondelsectorpublico.gob.es/sindicacion/sindicacion_643/licitacionesPerfilesContratanteCompleto3"
SOURCE = "CONTRATACION"
DEFAULT_LOOKBACK_DAYS = 30
MAX_LIVE_PAGES = 20
MAX_ZIP_YEARS = 5

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "cbc": "urn:dgpe:names:draft:codice:schema:xsd:CommonBasicComponents-2",
    "cac": "urn:dgpe:names:draft:codice:schema:xsd:CommonAggregateComponents-2",
    "cac-place-ext": "urn:dgpe:names:draft:codice-place-ext:schema:xsd:CommonAggregateComponents-2",
    "cbc-place-ext": "urn:dgpe:names:draft:codice-place-ext:schema:xsd:CommonBasicComponents-2",
}

def _parse_date(s, default):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return default

def search_contratacion(target, target_type, date_from, date_to, count):
    desde = _parse_date(date_from, datetime.now().date() - timedelta(days=DEFAULT_LOOKBACK_DAYS))
    hasta = _parse_date(date_to, datetime.now().date())
    days = (hasta - desde).days
    if days > 0 and (hasta - desde).days > 90:
        return _search_zip_files(target, target_type, desde, hasta, count)
    return _search_live_atom(target, target_type, desde, hasta, count)

def _matches_target(entry_nif, entry_name, target, target_type):
    norm_target = normalize_for_match(target)
    if target_type in ("nif", "cif"):
        return entry_nif and (entry_nif.strip().upper() == target.strip().upper())
    if target_type == "name":
        return entry_name and (norm_target in normalize_for_match(entry_name) or normalize_for_match(entry_name) in norm_target or norm_target in normalize_for_match(entry_name))
    return False

def _search_live_atom(target, target_type, desde, hasta, count):
    results = []
    feed_url = LIVE_FEED
    pages = 0
    while len(results) < count and pages < MAX_LIVE_PAGES and feed_url:
        resp = request_with_retry("GET", feed_url, timeout=30, headers={"Accept": "application/atom+xml"})
        if resp.status_code != 200:
            return None, f"PLACSP feed HTTP {resp.status_code}"
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError as e:
            logger.error("atom_parse_failed", extra_data={"error": str(e)})
            return None, "Error parsing ATOM feed"
        for entry in root.findall("atom:entry", NS):
            ev = _parse_entry(entry, target, target_type)
            if ev and _matches_target(
                ev["metadata"].get("adjudicatario_nif",""),
                ev["metadata"].get("adjudicatario_nombre",""),
                target, target_type):
                results.append(ev)
                if len(results) >= count:
                    break
        next_link = root.find('atom:link[@rel="next"]', NS)
        feed_url = next_link.get("href") if next_link is not None else None
        pages += 1
        if feed_url and "next" in (next_link.get("rel","") if next_link is not None else ""):
            pass
    return results, None

def _search_zip_files(target, target_type, desde, desde_hasta, count):
    results = []
    tmpdir = tempfile.mkdtemp(prefix="placsp_")
    try:
        years = sorted(set(range(desde.year, hasta.year + 1)))
        if len(years) > MAX_ZIP_YEARS:
            return None, f"Rango demasiado amplio: {len(years)} años (máx {MAX_ZIP_YEARS}). Use fechas más próximas."
        for year in years:
            if len(results) >= count:
                break
            zip_url = f"{ZIP_BASE}_{year}.zip"
            logger.info("downloading_zip", extra_data={"url": zip_url, "year": year})
            resp = request_with_retry("GET", zip_url, timeout=60, stream=True)
            if resp.status_code != 200:
                logger.warning("zip_download_failed", extra_data={"url": zip_url, "status": resp.status_code})
                continue
            try:
                zf = zipfile.ZipFile(io.BytesIO(resp.content))
            except zipfile.BadZipFile:
                logger.warning("bad_zip", extra_data={"url": zip_url})
                continue
            zf.extractall(tmpdir)
            for fname in sorted(os.listdir(tmpdir)):
                if not fname.endswith(".atom"):
                    continue
                fpath = os.path.join(tmpdir, fname)
                tree = ET.parse(fpath)
                for entry in tree.getroot().findall("atom:entry", NS):
                    ev = _parse_entry(entry, target, target_type)
                    if ev and _matches_target(
                        ev["metadata"].get("adjudicatario_nif",""),
                        ev["metadata"].get("adjudicatario_nombre",""),
                        target, target_type):
                        results.append(ev)
                        if len(results) >= count:
                            break
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return results, None

def _parse_entry(entry_elem, query, target_type):
    # parse one <entry> into Evidence; return None si no aplica
    title = entry_elem.findtext("atom:title", "", NS) or ""
    summary = entry_elem.findtext("atom:summary", "", NS) or ""
    link_el = entry_elem.find('atom:link[@rel="alternate"]', NS) or entry_elem.find("atom:link", NS)
    url = link_el.get("href") if link_el is not None else ""
    updated = entry_elem.findtext("atom:updated", "", NS) or ""
    pub_date = updated[:10] if updated else ""
    # CODICE nested fields
    cfs = entry_elem.find("cac-place-ext:ContractFolderStatus", NS)
    nif = ""
    nombre = ""
    importe = ""
    if cfs is not None:
        nif_el = cfs.find(".//cbc-place-ext:PartyTaxInformation/cbc:CompanyID", NS)
        nif = nif_el.text or "" if nif_el is not None else ""
        name_el = cfs.find(".//cac:PartyName/cbc:Name", NS)
        nombre = name_el.text or "" if name_el is not None else ""
        impel = cfs.find(".//cbc-place-ext:EstimatedAmount", NS)
        importe = impel.text or "" if impel is not None else ""
    return build_evidence(
        SOURCE, True, 1.0, title, pub_date, url,
        {"nif_beneficiario": nif, "adjudicatario_nombre": nombre,
         "importe_estimado": importe, "resumen": summary[:500]},
        query=query,
    )

# write_response + main() identical patrón to bdns_search (reuso código exacto)
# ... (igual que Task 4: main(), write_response(), manejo de request_id, JSONDecodeError, Exception)
```

- [ ] **Step 3: Smoke test**
```bash
echo '{"request_id":"t","arguments":{"target":"Banco Santander","target_type":"name","date_from":"2025-01-01","date_to":"2026-01-01"}}' | python3 tools/contratacion_search/main.py
```
Expected: `{"success":true,...}` con `results` (la API live devuelve datos reales verificados).

- [ ] **Step 4: Fix errors from smoke output**

- [ ] **Step 5: Commit**
```bash
git add tools/contratacion_search/main.py tools/contratacion_search/tool.yaml
git commit -m "feat: complete contratacion_search with hybrid ATOM feed"
```

---

### Task 6: New `doue_search` (EUR-Lex SOAP)

**Files:**
- Create: `tools/doue_search/tool.yaml`, `tools/doue_search/main.py`

**Interfaces:**
- Consumes: shared helpers + stdlib `xml.etree`, `xml.sax.saxutils.escape`.
- Produces: `structured_content` con `results:[Evidence...]`.

- [ ] **Step 1: Create `tool.yaml`**
```yaml
name: doue_search
description: "Busca menciones en el Diario Oficial de la UE (EUR-Lex) por NIF/nombre. API SOAP pública."
command: python3
args: [main.py]
timeout: 90s
input_schema:
  type: object
  properties:
    target: {type: string}
    target_type: {type: string, enum: [nif, cif, name], default: name}
    date_from: {type: string}
    date_to: {type: string}
    count: {type: integer, default: 20}
  required: [target]
```

- [ ] **Step 2: Write `main.py`**

```python
#!/usr/bin/env python3
import json, os, sys, hashlib
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape as xml_escape
from typing import Any, Optional
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common.structured_logging import get_logger
from common.http import request_with_retry
from common.evidence import build_evidence
from common.entity_normalizer import normalize_for_match

logger = get_logger(__name__, "doue_search")
WSDL_URL = "https://eur-lex.europa.eu/EURLexWebService"
SOAP_ACTION = "http://eur-lex.europa.eu/search/GetNoticeList"
SOURCE = "DOUE"
DEFAULT_LOOKBACK_DAYS = 365

def _parse_date(s, default):
    try:
        return datetime.strptime(s, "%Y-%m-%d")
    except (ValueError, TypeError):
        return default

def search_doue(target, target_type, date_from, date_to, count):
    desde = _parse_date(date_from, datetime.now() - timedelta(days=DEFAULT_LOOKBACK_DAYS))
    hasta = _parse_date(date_to, datetime.now())
    query_parts = []
    if target_type in ("nif","cif"):
        query_parts.append(f"DN={target}")
    else:
        query_parts.append(f'DN="{xml_escape(target)}"')
    query_parts.append(f'DD >= {desde.year}{desde.month:02d}{desde.day:02d}')
    query_parts.append(f'DD <= {hasta.year}{hasta.month:02d}{hasta.day:02d}')
    expr = " AND ".join(query_parts)
    max_results = min(count, 100)
    envelope = f"""<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <GetNoticeList xmlns="http://eur-lex.europa.eu/search">
      <query>{xml_escape(expr)}</query>
      <resultScope>FULL</resultScope>
      <maxResults>{max_results}</maxResults>
    </GetNoticeList>
  </soap:Body>
</soap:Envelope>"""
    resp = request_with_retry("POST", WSDL_URL, headers={
        "Content-Type":"text/xml; charset=utf-8",
        "SOAPAction": SOAP_ACTION,
    }, timeout=60, data=envelope)
    if resp.status_code != 200:
        return None, f"EUR-Lex SOAP HTTP {resp.status_code}"
    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError:
        return None, "Error parsing SOAP response"
    results = []
    # path: //GetNoticeListResponse/notices/notice
    notices = root.findall(".//notice")
    if not notices:
        notices = root.findall(".//{*}notice")
    for notice in notices:
        celex = notice.findtext("celexIdentifier", "") or notice.findtext(".//celex", "") or ""
        title = notice.findtext("title", "") or ""
        doc_type = notice.findtext("documentType", "") or ""
        url = f"https://eur-lex.europa.eu/legal-content/ES/TXT/?uri=CELEX:{celex}" if celex else ""
        pub_date = notice.findtext("publicationDate", "") or ""
        oj = notice.findtext("ojReference", "") or ""
        results.append(build_evidence(
            SOURCE, official=True, confidence=0.9,
            title=title, date=pub_date, url=url,
            metadata={"celex": celex, "document_type": doc_type, "oj_reference": oj},
            query=target,
        ))
        if len(results) >= count:
            break
    return results, None

# write_response + main() — patrón idéntico a Task 4
# (se reusa la estructura de main(): validate target, call search_doue, format markdown,
#  return content + structured_content con official, confidence, results, count)
```

- [ ] **Step 3: Register in `toolsets.yaml`** (+ 1 línea)

- [ ] **Step 4: Smoke test**
```bash
echo '{"request_id":"t","arguments":{"target":"Banco Santander","target_type":"name","date_from":"2025-01-01","date_to":"2026-01-01"}}' | python3 tools/doue_search/main.py
```
Expected: `{"success":true,...}` o `SEARCH_FAILED` con detalle SOAP (si el query tiene issue).

- [ ] **Step 5: Commit**
```bash
mkdir tools/doue_search
git add tools/doue_search/tool.yaml tools/doue_search/main.py configs/toolsets.yaml
git commit -m "feat: add doue_search tool (EUR-Lex SOAP)"
```

---

### Task 7: New `transparency_search` (scraping Portal Transparencia)

**Files:**
- Create: `tools/transparency_search/tool.yaml`, `tools/transparency_search/main.py`

**Interfaces:**
- Consumes: shared helpers + stdlib `html.parser`.
- Produces: `structured_content` con `results:[Evidence...]`.

- [ ] **Step 1: Create `tool.yaml`**
```yaml
name: transparency_search
description: "Busca en Portal de Transparencia: publicidad activa + Altos Cargos."
command: python3
args: [main.py]
timeout: 120s
input_schema:
  type: object
  properties:
    target: {type: string}
    target_type: {type: string, enum: [nif, cif, name], default: name}
    search_scope: {type: string, enum: [all, publicidad_activa, altos_cargos], default: all}
    date_from: {type: string}
    date_to: {type: string}
    count: {type: integer, default: 20}
  required: [target]
```

- [ ] **Step 2: Write `main.py`**

```python
#!/usr/bin/env python3
import json, os, sys, re, html as html_lib
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from html.parser import HTMLParser
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common.structured_logging import get_logger
from common.http import request_with_retry
from common.evidence import build_evidence
from common.entity_normalizer import normalize_for_match

logger = get_logger(__name__, "transparency_search")
BASE_URL = "https://www.transparencia.gob.es"
BUSCADOR_URL = f"{BASE_URL}/servicios-buscador/buscar.htm"
SOURCE = "TRANSPARENCIA"
MAX_PAGES = 10

class TransparenciaParser(HTMLParser):
    """Extract result rows from buscador HTML."""
    def __init__(self):
        super().__init__()
        self.results = []
        self._in_result = False
        self._current = {}
        self._capture = None
    def handle_starttag(self, tag, attrs):
        attrs_d = dict(attrs)
        cls = attrs_d.get("class", "")
        if "resultado" in cls or "result-item" in cls:
            self._in_result = True
            self._current = {}
        if self._in_result and tag in ("a", "h3", "h4", "p", "span", "div"):
            cls_lower = cls.lower() if cls else ""
            field = None
            if "titulo" in cls_lower or "title" in cls_lower:
                field = "title"
            elif "fecha" in cls_lower or "date" in cls_lower:
                field = "date"
            elif "organismo" in cls_lower:
                field = "organismo"
            elif "importe" in cls_lower or "monto" in cls_lower:
                field = "importe"
            elif tag == "a" and "href" in attrs_d and "title" in self._current:
                self._current["url"] = attrs_d["href"]
            if field:
                self._capture = field
    def handle_data(self, data):
        if self._capture and self._in_result:
            self._current[self._capture] = html_lib.unescape(data.strip())
    def handle_endtag(self, tag):
        if self._capture:
            self._capture = None
        if tag in ("div","li","article") and self._in_result and self._current:
            if any(self._current.values()):
                self.results.append(self._current)
                self._current = {}
                self._in_result = False
```

NOTA: el parser usa `html.parser` (stdlib). El Portal de Transparencia usa clases `.resultado-*` semánticas. Si el layout cambia, el parser devuelve `[]` con log `PARSE_WARNING` (NO regex fallback). Smoke test en runtime verifica.

```python
def _search_publicidad_activa(target, date_from, date_to, count):
    results = []
    page = 1
    norm_target = normalize_for_match(target)
    while len(results) < count and page <= MAX_PAGES:
        params = {"q": target, "page": page}
        if date_from: params["fechaDesde"] = date_from.replace("-","")
        if date_to: params["fechaHasta"] = date_to.replace("-","")
        resp = request_with_retry("GET", BUSCADOR_URL, params=params, timeout=30)
        if resp.status_code != 200:
            logger.warning("transparencia_scrape_failed", extra_data={"page": page, "status": resp.status_code})
            break
        parser = TransparenciaParser()
        parser.feed(resp.text)
        if not parser.results:
            logger.parse_warning()
            break
        for r in parser.results:
            title = r.get("title","") or ""
            if norm_target and (norm_target in normalize_for_match(title) or normalize_for_match(title) in norm_target):
                results.append(build_evidence(
                    SOURCE, True, 0.9, title, r.get("date",""), r.get("url",""),
                    {"organismo": r.get("organismo",""), "importe": r.get("importe","")}, query=target,
                ))
                if len(results) >= count:
                    break
        if len(parser.results) < 10:  # last page signal
            break
        page += 1
    return results
```

`_search_altos_cargos` sigue patrón similar sobre `/publicidad-activa/por-materias/altos-cargos/{curriculos|actividad-privada-cese}` con parámetro `?q=target`. Si `target_type` es `nif`/`cif` → devuelve `[]` con warning (Altos Cargos no indexa por NIF).

- [ ] **Step 3: Register in `toolsets.yaml`**

- [ ] **Step 4: Smoke test**
```bash
echo '{"request_id":"t","arguments":{"target":"Banco Santander","target_type":"name","date_from":"2025-01-01","date_to":"2026-01-01"}}' | python3 tools/transparency_search/main.py
```

- [ ] **Step 5: Commit**
```bash
mkdir tools/transparency_search
git add tools/transparency_search/ configs/toolsets.yaml
git commit -m "feat: add transparency_search tool"
```

---

### Task 8: Go orchestrator + tests (`investigate.go` + `investigate_test.go`)

**Files:**
- Modify: `internal/orchestrator/investigate.go`
- Reescribir: `internal/orchestrator/investigate_test.go`

**Interfaces:**
- `InvestigateRequest{target, target_type, sources, date_from, date_to}` (existe).
- `SourceResult{source, success, error, data}` (existe).
- `o.cfg.Tools []config.ToolConfig` (existe, post-toolset-filter en config.go:333).

- [ ] **Step 1: Write failing Go test first (TDD)**

`tests` no existe como dir Go separado; sigue patrón repo: `internal/orchestrator/investigate_test.go`. Nuevo test `TestInvestigateFanOutAllSearchTools`:

```go
func TestInvestigateFanOutAllSearchTools(t *testing.T) {
    mockExec := &mockExecutor{
        responses: map[string]mcptypes.SubprocessResponse{
            "ted_search":          {Success: true, StructuredContent: map[string]interface{}{"source":"TED","count":3}},
            "borme_search":        {Success: true, StructuredContent: map[string]interface{}{"source":"BORME","count":2}},
            "boe_search":          {Success: true, StructuredContent: map[string]interface{}{"source":"BOE","count":1}},
            "bdns_search":         {Success: true, StructuredContent: map[string]interface{}{"source":"BDNS","count":4}},
            "contratacion_search": {Success: true, StructuredContent: map[string]interface{}{"source":"CONTRATACION","count":5}},
            "doue_search":         {Success: true, StructuredContent: map[string]interface{}{"source":"DOUE","count":2}},
            "transparency_search": {Success: true, StructuredContent: map[string]interface{}{"source":"TRANSPARENCIA","count":3}},
            "searxng_search":      {Success: true, StructuredContent: map[string]interface{}{"source":"SEARXNG","count":6}},
        },
        receivedArgs: make([]map[string]interface{}, 0, 8),
    }
    cfg := &config.Config{
        Tools: []config.ToolConfig{
            {Name: "ted_search"}, {Name: "borme_search"}, {Name: "boe_search"},
            {Name: "bdns_search"}, {Name: "contratacion_search"}, {Name: "doue_search"},
            {Name: "transparency_search"}, {Name: "searxng_search"},
            {Name: "journalist_investigate"}, // must be EXCLUDED
        },
    }
    orch := New(cfg, mockExec)
    result := orch.Investigate(context.Background(), InvestigateRequest{
        Target: "Banco Santander", DateFrom: "2025-01-01", DateTo: "2026-01-01",
    })
    if len(result.SourceResults) != 8 {
        t.Fatalf("expected 8 source results, got %d", len(result.SourceResults))
    }
    // verify each source present, no duplicates, no journalist_investigate
    seen := map[string]bool{}
    for _, sr := range result.SourceResults {
        if sr.Source == "journalist_investigate" {
            t.Errorf("orchestrator should not invoke itself")
        }
        if seen[sr.Source] { t.Errorf("duplicate source %s", sr.Source) }
        seen[sr.Source] = true
    }
    expectedSources := map[string]bool{
        "ted_search":false,"borme_search":false,"boe_search":false,"bdns_search":false,
        "contratacion_search":false,"doue_search":false,"transparency_search":false,"searxng_search":false,
    }
    for s := range expectedSources { _ = s }
    if len(seen) != 8 { t.Errorf("expected 8 unique sources, got %d", len(seen)) }
    // verify date propagation
    for _, args := range mockExec.receivedArgs {
        if args["date_from"] != "2025-01-01" { t.Errorf("date_from not propagated") }
    }
}
```

Requiere `mockExecutor` (implementa `executor.Executor` interface). Define en mismo test file.

- [ ] **Step 2: Run test to verify it FAILS**
Expected: `defaultSources` is hardcoded; mock cfg not used → only 4 tools fire → `len(SourceResults) != 8`.

- [ ] **Step 3: Change `investigate.go`**

```go
// REMOVE hardcoded var defaultSources = []string{...}
// ADD:
func (o *Orchestrator) resolveSources(requested []string) []string {
    if len(requested) > 0 {
        return requested
    }
    var out []string
    for _, t := range o.cfg.Tools {
        if strings.HasSuffix(t.Name, "_search") {
            out = append(out, t.Name)
        }
    }
    return out
}
```
Y en `Investigate`: `sources := req.Sources;` → `sources := o.resolveSources(req.Sources);`. Añadir `import "strings"`.

- [ ] **Step 4: Run test to verify it PASSES**
```bash
go test ./internal/orchestrator/... -run TestInvestigate -v
```
Expected: PASS.

- [ ] **Step 5: Run full verification + commit**
```bash
go vet ./... && go build ./cmd/server/
git add internal/orchestrator/investigate.go internal/orchestrator/investigate_test.go
git commit -m "refactor: derive orchestrator default sources dynamically from toolset config"
```

### Task 9: Migrate existing tools a `common/http.py`

**Files (modify):**
- `tools/boe_search/main.py`
- `tools/borme_search/main.py`
- `tools/ted_search/main.py`
- `tools/searxng_search/main.py`

**Proceso por tool (drop-in)**:
- [ ] En cada `main.py` reemplazar `import requests` por `from common.http import request_with_retry` (mantener try/except fallback a `requests`).
- [ ] Reemplazar `requests.get(url, params=..., timeout=..., headers=...)` → `request_with_retry("GET", url, params=..., headers=..., timeout=...)`.
- [ ] Reemplazar `requests.post(url, json=..., timeout=...)` → `request_with_retry("POST", url, json=..., timeout=...)`.
- [ ] Smoke test de cada tool: `echo '{"request_id":"t","arguments":{"query":"prueba","count":1}}' | python3 tools/<tool>/main.py` → `{success:true}`.

```bash
git add tools/boe_search/main.py tools/borme_search/main.py tools/ted_search/main.py tools/searxng_search/main.py
git commit -m "refactor: migrate boe/borme/ted/searxng to common/http.py"
```

---

## Orden de ejecución

1. Task 1 → Task 2 → Task 3 (helpers, base común, sin dependencias)
2. Task 4 + Task 5 (tools que dependen de helpers) — paralelizables
3. Task 6 + Task 7 (new tools, dependen de helpers) — paralelizables
4. Task 9 (migración existentes) — paralelizable, después de Task 1
5. Task 8 (Go) — último, integra todo

Tasks 4, 5, 6, 7, 9 pueden ejecutarse en paralelo entre subagents (cada uno modifica directorios distintos).
