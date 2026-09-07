# Completar OSINT-ES — Tasks 7, 8, 9

> **ESTADO:** COMPLETADO. Este documento se conserva como histórico. Las tareas se implementaron y pushearon a `master` en agosto 2026.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Completar las 3 tareas restantes del plan OSINT-ES: crear `transparency_search` (Task 7), dinamizar el orquestador Go (Task 8), y migrar 4 tools existentes a `common/http.py` (Task 9).

**Architecture:** Cada tool es un subproceso Python stateless con contrato JSON stdin/stdout. El orquestador Go (`journalist_investigate`) debe derivar `defaultSources` dinámicamente de `o.cfg.Tools` filtrando por sufijo `_search` (en lugar del hardcode actual de 4 fuentes). La migración de Task 9 sustituye `requests.*` directo por `request_with_retry` de `common/http.py`, manteniendo compatibilidad drop-in.

**Tech Stack:** Python 3 (`requests` + stdlib: `html.parser`), Go 1.23 (orquestador), YAML manifests.

## Global Constraints

- Go 1.23.0 (module pinned).
- Python tools: `requests` + stdlib. Nada de `BeautifulSoup`.
- Response contract: stdout = 1 JSON object; stderr = structured logs via `common/structured_logging.get_logger`.
- `tool.yaml` discovery es automático; no se necesita registro Go para tools Python.
- `internal/orchestrator/investigate.go:44-46` `defaultSources` DEBE reemplazarse por `resolveSources()` dinámico.
- Todo texto `content` en español. Campos estructurados/JSON en inglés.
- Cada `main.py` usa `sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))` + `from common.*`.

---

### Task 7: Crear `transparency_search` (Portal de Transparencia)

**Files:**
- Create: `tools/transparency_search/tool.yaml`
- Create: `tools/transparency_search/main.py`
- Create: `tests/tools/test_transparency_search.py`
- Modify: `configs/toolsets.yaml`

**Interfaces:**
- Consumes: `common.http.request_with_retry`, `common.evidence.build_evidence`, `common.entity_normalizer.normalize_for_match`, `common.structured_logging.get_logger`.
- Produces: `structured_content` con `{source:"TRANSPARENCIA", official:true, confidence:0.9, results:[Evidence...], count:N}`.

- [x] **Step 1: Crear `tool.yaml`**

```yaml
name: transparency_search
description: "Busca en el Portal de Transparencia: publicidad activa y Altos Cargos por nombre."
command: python3
args: [main.py]
timeout: 120s
input_schema:
  type: object
  properties:
    target:
      type: string
      description: "Nombre de la persona o entidad a buscar"
    target_type:
      type: string
      enum: [name]
      default: name
      description: "Solo búsqueda por nombre (el portal no indexa por NIF/CIF)"
    search_scope:
      type: string
      enum: [all, publicidad_activa, altos_cargos]
      default: all
    date_from:
      type: string
      description: "YYYY-MM-DD"
    date_to:
      type: string
      description: "YYYY-MM-DD"
    count:
      type: integer
      default: 20
  required: [target]
```

- [x] **Step 2: Crear `main.py`** — parser + lógica de búsqueda

```python
#!/usr/bin/env python3
"""Portal de Transparencia search tool.

Scrapea el buscador de publicidad activa del Portal de Transparencia
(https://www.transparencia.gob.es) usando html.parser stdlib.

Estrategia:
  - GET a /servicios-buscador/buscar.htm con ?q=target
  - Parsear resultados con HTMLParser (clases .resultado-*)
  - Si el layout cambia, devolver [] con log PARSE_WARNING (sin regex fallback)
  - Altos Cargos: solo por nombre (el portal no indexa por NIF/CIF)
"""

import json
import os
import sys
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common.structured_logging import get_logger
from common.http import request_with_retry, REQUESTS_AVAILABLE
from common.evidence import build_evidence
from common.entity_normalizer import normalize_for_match

logger = get_logger(__name__, "transparency_search")

BASE_URL = "https://www.transparencia.gob.es"
BUSCADOR_URL = f"{BASE_URL}/servicios-buscador/buscar.htm"
SOURCE = "TRANSPARENCIA"
MAX_PAGES = 10


class TransparenciaParser(HTMLParser):
    """Extrae filas de resultados del HTML del buscador del Portal."""

    def __init__(self):
        super().__init__()
        self.results: list[dict[str, str]] = []
        self._in_result = False
        self._current: dict[str, str] = {}
        self._capture: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        attrs_d = dict(attrs)
        cls = (attrs_d.get("class") or "").lower()
        if any(k in cls for k in ("resultado", "result-item", "resultado-buscador")):
            self._in_result = True
            self._current = {}
        if not self._in_result:
            return
        if tag == "a" and "href" in attrs_d:
            href = attrs_d["href"]
            if href and not href.startswith("#"):
                self._current["url"] = urljoin(BASE_URL, href)
        field = None
        if "titulo" in cls or "title" in cls:
            field = "title"
        elif "fecha" in cls or "date" in cls:
            field = "date"
        elif "organismo" in cls or "organo" in cls:
            field = "organismo"
        elif "importe" in cls or "monto" in cls or "cantidad" in cls:
            field = "importe"
        elif "descripcion" in cls or "description" in cls:
            field = "description"
        if field:
            self._capture = field

    def handle_data(self, data: str):
        if self._capture and self._in_result:
            text = data.strip()
            if text:
                prev = self._current.get(self._capture, "")
                self._current[self._capture] = (prev + " " + text).strip()

    def handle_endtag(self, tag: str):
        if tag in ("div", "li", "article") and self._in_result and self._current:
            if any(self._current.values()):
                self.results.append(dict(self._current))
            self._current = {}
            self._in_result = False
        self._capture = None


def _search_publicidad_activa(
    target: str, date_from: str, date_to: str, count: int,
) -> tuple[list[dict], str | None]:
    results: list[dict] = []
    norm_target = normalize_for_match(target)
    page = 1
    while len(results) < count and page <= MAX_PAGES:
        params: dict[str, Any] = {"q": target, "page": page}
        if date_from:
            params["fechaDesde"] = date_from.replace("-", "")
        if date_to:
            params["fechaHasta"] = date_to.replace("-", "")
        resp = request_with_retry(
            "GET", BUSCADOR_URL, params=params, timeout=30,
            headers={"Accept": "text/html"},
        )
        if resp.status_code != 200:
            logger.warning(
                "transparencia_scrape_failed",
                extra_data={"page": page, "status": resp.status_code},
            )
            break
        parser = TransparenciaParser()
        parser.feed(resp.text)
        if not parser.results:
            logger.warning("PARSE_WARNING: no se encontraron resultados HTML")
            break
        for r in parser.results:
            title = r.get("title", "")
            if not title:
                continue
            if norm_target and (
                norm_target in normalize_for_match(title)
                or normalize_for_match(title) in norm_target
            ):
                ev = build_evidence(
                    source=SOURCE,
                    official=True,
                    confidence=0.9,
                    title=title,
                    date=r.get("date", ""),
                    url=r.get("url", ""),
                    metadata={
                        "organismo": r.get("organismo", ""),
                        "importe": r.get("importe", ""),
                        "descripcion": r.get("description", ""),
                    },
                    query=target,
                )
                results.append(ev)
                if len(results) >= count:
                    break
        if len(parser.results) < 10:
            break
        page += 1
    return results, None


def search_transparency(
    target: str, _target_type: str, search_scope: str,
    date_from: str, date_to: str, count: int,
) -> tuple[list[dict] | None, str | None]:
    if not REQUESTS_AVAILABLE:
        return None, "la librería 'requests' no está disponible"
    results: list[dict] = []
    if search_scope in ("all", "publicidad_activa"):
        pa_results, err = _search_publicidad_activa(
            target, date_from, date_to, count,
        )
        if err:
            return None, err
        results.extend(pa_results)
    # Altos Cargos: endpoint distinto, solo por nombre. El spec lo documenta
    # como posible extensión futura. Por ahora solo publicidad_activa.
    if search_scope in ("all", "altos_cargos") and len(results) < count:
        logger.warning(
            "altos_cargos_no_implementado",
            extra_data={"msg": "Altos Cargos endpoint requiere investigación adicional"},
        )
    return results[:count], None


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
                "success": False, "request_id": request_id,
                "error": {"code": "MISSING_TARGET",
                          "message": "el parámetro 'target' es obligatorio"},
            })
            return
        search_scope = args.get("search_scope", "all")
        date_from = args.get("date_from", "")
        date_to = args.get("date_to", "")
        count_raw = args.get("count", 20)
        try:
            count = int(count_raw)
        except (TypeError, ValueError):
            count = 20
        if count <= 0:
            count = 20

        results, error = search_transparency(
            target, "name", search_scope, date_from, date_to, count,
        )
        if error:
            write_response({
                "success": False, "request_id": request_id,
                "error": {"code": "SEARCH_FAILED", "message": error},
            })
            return

        retrieved_at = datetime.now(timezone.utc).isoformat()
        lines = [
            f"**Portal de Transparencia — Resultados para \"{target}\"**\n",
        ]
        if not results:
            lines.append("No se encontraron resultados.")
        else:
            for i, r in enumerate(results, 1):
                m = r.get("metadata", {}) or {}
                lines.append(f"{i}. **{r.get('title')}**")
                if r.get("date"):
                    lines.append(f"   - Fecha: {r['date']}")
                if m.get("organismo"):
                    lines.append(f"   - Organismo: {m['organismo']}")
                if m.get("importe"):
                    lines.append(f"   - Importe: {m['importe']}")
                if r.get("url"):
                    lines.append(f"   - URL: {r['url']}")
                lines.append("")

        write_response({
            "success": True, "request_id": request_id,
            "content": [{"type": "text", "text": "\n".join(lines)}],
            "structured_content": {
                "source": SOURCE, "official": True, "confidence": 0.9,
                "results": results, "count": len(results),
                "retrieved_at": retrieved_at, "query": target,
            },
        })
    except json.JSONDecodeError:
        write_response({
            "success": False, "request_id": "",
            "error": {"code": "INVALID_JSON",
                      "message": "No se pudo analizar el JSON de entrada"},
        })
    except Exception as exc:
        logger.error("Excepción no gestionada", extra_data={"error": str(exc)})
        write_response({
            "success": False, "request_id": request.get("request_id", ""),
            "error": {"code": "EXECUTION_FAILED", "message": str(exc)},
        })


if __name__ == "__main__":
    main()
```

- [x] **Step 3: Smoke test manual**

```bash
echo '{"request_id":"t","arguments":{"target":"Banco Santander","date_from":"2025-01-01","date_to":"2026-01-01"}}' | python3 tools/transparency_search/main.py
```

- [x] **Step 4: Crear `tests/tools/test_transparency_search.py`**

Siguiendo el patrón de `test_bdns_search.py` (subprocess smoke test).

- [x] **Step 5: Registrar en `configs/toolsets.yaml`**

Añadir `- transparency_search` en los toolsets `osint-es` (después de `transparency_fetch`) y `ocu-investigacion` (sección OSINT español).

- [x] **Step 6: Commit**

```bash
git add tools/transparency_search/tool.yaml tools/transparency_search/main.py \
        tests/tools/test_transparency_search.py configs/toolsets.yaml
git commit -m "feat: add transparency_search tool (Portal de Transparencia scraping)"
```

---

### Task 8: Go orquestador dinámico

**Files:**
- Modify: `internal/orchestrator/investigate.go:44-52`
- Modify: `internal/orchestrator/investigate_test.go` (reescritura parcial)

**Interfaces:**
- Consumes: `o.cfg.Tools []config.ToolConfig` (Name field, existe), `cfg.GetToolByName(name)` (existe en `config.go:401`).
- Produces: `o.resolveSources(requested []string) []string` — nuevo método privado.

- [x] **Step 1: Escribir el test Go que debe fallar**

Añadir a `internal/orchestrator/investigate_test.go` `TestInvestigateDefaultSourcesDynamic` que espera fan-out a 8 tools `*_search` (excluye `journalist_investigate`).

- [x] **Step 2: Ejecutar test para verificar que FALLA**

```bash
go test ./internal/orchestrator/... -run TestInvestigateDefaultSourcesDynamic -v
```

- [x] **Step 3: Cambiar `investigate.go`**

Reemplazar `var defaultSources = []string{...}` por `resolveSources(requested []string) []string` que deriva de `o.cfg.Tools` filtrando por sufijo `_search` y excluyendo `journalist_investigate`. Añadir `import "strings"`.

- [x] **Step 4: Ejecutar tests para verificar que PASAN**

```bash
go test ./internal/orchestrator/... -v
```

- [x] **Step 5: Verificación Go completa**

```bash
go vet ./...
go build ./cmd/server/
```

- [x] **Step 6: Commit**

```bash
git add internal/orchestrator/investigate.go internal/orchestrator/investigate_test.go
git commit -m "refactor: derive orchestrator default sources dynamically from tool config"
```

---

### Task 9: Migrar 4 tools existentes a `common/http.py`

**Files:**
- Modify: `tools/boe_search/main.py`
- Modify: `tools/borme_search/main.py`
- Modify: `tools/ted_search/main.py`
- Modify: `tools/searxng_search/main.py`

**Interfaces:**
- Consumes: `common.http.request_with_retry(method, url, params=..., json=..., headers=..., timeout=...) → requests.Response`.
- Cambio: reemplazar `requests.get(url, ...)` / `requests.post(url, ...)` por `request_with_retry("GET"/"POST", url, ...)`. Sin cambios en parsing ni en lógica.

- [x] **Step 1: Migrar `boe_search/main.py`** — reemplazar import requests por `from common.http import request_with_retry, REQUESTS_AVAILABLE`; `requests.get(url, headers, timeout)` → `request_with_retry("GET", url, headers, timeout)`.

- [x] **Step 2: Smoke test `boe_search`**

```bash
echo '{"request_id":"t","arguments":{"target":"Ministerio","target_type":"name","date_from":"2026-07-01","date_to":"2026-08-01"}}' | python3 tools/boe_search/main.py
```

- [x] **Step 3: Migrar `borme_search/main.py`** — mismos 2 cambios.

- [x] **Step 4: Smoke test `borme_search`**

- [x] **Step 5: Migrar `ted_search/main.py`** — reemplazar import + `requests.post(TED_API_URL, json=payload, timeout=30, headers)` → `request_with_retry("POST", ...)`; adaptar try/except (request_with_retry propaga exceptions).

- [x] **Step 6: Smoke test `ted_search`**

- [x] **Step 7: Migrar `searxng_search/main.py`** — reemplazar import + `requests.get(...)` → `request_with_retry("GET", ...)`; unificar excepts.

- [x] **Step 8: Smoke test `searxng_search`** (requiere SearXNG corriendo)

- [x] **Step 9: Verificación completa**

```bash
python3 -m pytest tests/ -q
go test ./...
```

- [x] **Step 10: Commit**

```bash
git add tools/boe_search/main.py tools/borme_search/main.py \
        tools/ted_search/main.py tools/searxng_search/main.py
git commit -m "refactor: migrate boe/borme/ted/searxng to common/http.py request_with_retry"
```

---

## Orden de ejecución

```
Task 7 (transparency_search)  +  Task 9 (migración 4 tools)  →  paralelo
                              ↓
                    Task 8 (Go dinámico)  →  último, integra todo
```

Realmente se ejecutó en dos tramos:
- Fase 4: contrato de evidencias, normalización de 7 fuentes `*_search`, agregación determinista, métricas por fuente y documentación.
- Migración completa de capa HTTP: además de las 4 tools del plan original, se migraron todas las tools Python restantes a `common/http.request_with_retry` y se añadieron soporte para `files=` y `allow_redirects=`.

## Plan de verificación final

- [x] `python3 -m pytest tests/ -q` — 62 tests Python pasan.
- [x] `go test ./...` — todos los paquetes Go pasan.
- [x] `go vet ./...` — sin errores.
- [x] `go build ./cmd/server/` — compila.
- [x] Trabajo pusheado a `origin/master`.

---

## Notas de cierre

- `transparency_search` cubre **publicidad_activa**; el scope `altos_cargos` queda bloqueado porque el portal no expone endpoint buscable (solo navegación jerárquica).
- `tools/common/retry.py` mantiene `requests` directo a propósito: es un wrapper de `tenacity` para LLMs, no encaja en `request_with_retry`.
