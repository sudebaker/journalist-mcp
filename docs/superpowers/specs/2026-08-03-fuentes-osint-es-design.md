# Diseño: Completar fuentes OSINT-ES y añadir fuentes europeas/nacionales

## 1. Visión

Completar el toolset `osint-es` de `journalist-mcp` para que `journalist_investigate { target, date_from, date_to }` cubra **9 fuentes oficiales** en paralelo: TED, BORME, BOE, BDNS, Contratación, **DOUE**, **Transparencia**, searxng y browserless. Hoy solo 5 fuentes devuelven datos reales; el proyecto agrega 2 fuentes nuevas y completa las 2 fuentes "skeleton".

## 2. Alcance

| Estado | Módulo | Tipo | Fuente | Protocolo |
|---|---|---|---|---|
| Completar | `tools/bdns_search` | subvenciones | BDNS | API REST JSON pública |
| Completar | `tools/contratacion_search` | licitaciones | PLACSP | Feed ATOM (CODICE/UBL), híbrido live + zip |
| Nuevo | `tools/doue_search` | actos UE | EUR-Lex | SOAP/WSDL |
| Nuevo | `tools/transparency_search` | transparencia | Portal Transparencia | Scraping HTML (publicidad activa + altos cargos) |

## 3. Arquitectura

Patrón existente (cambios mínimos en Go):
- Subprocess Python (`python3 main.py`) descubierto vía `tool.yaml` en `tools/`, leído por `internal/config/discovery.go`.
- Contrato JSON stdin/stdout definido en `internal/mcp/types.go`:
  - in:  `SubprocessRequest{request_id, tool_name, arguments, context}`
  - out: `{"success": true, "request_id": ..., "content": [{"type":"text","text":...}], "structured_content": {...}}`
- Log estructurado a stderr vía `tools/common/structured_logging.py` (sys.path parent trick).
- Orquestador `internal/orchestrator/investigate.go` ejecuta todos los `*_search` del toolset en paralelo con `errgroup`. **Requiere ~15 líneas de cambio en Go** (ver sección 3.1): `defaultSources` está hardcoded y debe derivarse dinámicamente de `o.cfg.Tools` filtrando por sufijo `_search` (excluyendo `journalist_investigate` que es el propio orquestador). Esto permite que añadir una fuente nueva solo requiera `toolsets.yaml`, sin tocar Go.

### Módulos afectados

| Componente | Cambio |
|---|---|
| `tools/bdns_search/main.py` | Reescribir stub → API JSON |
| `tools/bdns_search/tool.yaml` | + `date_from`, `date_to`, `count` |
| `tools/contratacion_search/main.py` | Reescribir stub → feed ATOM híbrido |
| `tools/contratacion_search/tool.yaml` | + `date_from`, `date_to`, `count` |
| `tools/doue_search/` | Crear `tool.yaml` + `main.py` |
| `tools/transparency_search/` | Crear `tool.yaml` + `main.py` |
| `tools/common/evidence.py` | Nuevo helper: modelo de evidencia común |
| `tools/common/http.py` | Nuevo helper: sesión con retry/backoff + Retry-After 429/503 |
| `tools/common/entity_normalizer.py` | Nuevo helper: normalización para matching |
| `tools/boe_search/main.py` | Migrar a `common.http.request_with_retry` (refactor coexistencia) |
| `tools/borme_search/main.py` | Migrar a `common.http.request_with_retry` |
| `tools/ted_search/main.py` | Migrar a `common.http.request_with_retry` |
| `tools/searxng_search/main.py` | Migrar a `common.http.request_with_retry` |
| `configs/toolsets.yaml` | + `doue_search`, `transparency_search` |
| `internal/orchestrator/investigate.go` | Derivar `defaultSources` dinámicamente (~15 líneas) |
| `internal/orchestrator/investigate_test.go` | Reescribir: fan-out a 9 tools (~120 líneas) |

### Flujo

```
LLM → POST /mcp → Go executor → python3 main.py (stdin JSON) → fuente oficial
                                                ← (HTTP/scraping/SOAP)
                                              →  stdout JSON (SubprocessResponse)
                                    → Go executor → orquestador paralelo → LLM
```

## 4. Módulo: `bdns_search` (completar)

### URL base

```text
https://www.pap.hacienda.gob.es/bdnstrans/api/consulta-beneficiarios
```

### `tool.yaml`

```yaml
name: bdns_search
description: "Busca subvenciones en BDNS (Base de Datos Nacional de Subvenciones) por beneficiario (NIF/CIF/nombre). API REST JSON oficial."
command: python3
args: [main.py]
timeout: 120s
input_schema:
  type: object
  properties:
    target: {type: string, description: "NIF/CIF o nombre del beneficiario"}
    target_type: {type: string, enum: [nif, cif, name], default: nif}
    date_from: {type: string, description: "YYYY-MM-DD. Default: 1 año atrás."}
    date_to: {type: string, description: "YYYY-MM-DD. Default: hoy."}
    count: {type: integer, default: 20, description: "Máx resultados (default 20)"}
  required: [target]
```

### `main.py` — contrato

```python
import requests
from common.structured_logging import get_logger
logger = get_logger(__name__, "bdns_search")

BDNS_API = "https://www.pap.hacienda.gob.es/bdnstrans/api/consulta-beneficiarios"
DEFAULT_TIMEOUT = 30

def search_bdns(target, target_type, date_from, date_to, count):
    # requests.get con params query params:
    #   - nifBeneficiario  | nombreBeneficiario  (según target_type)
    #   - fechaDesdeConcesion (YYYYMMDD)
    #   - fechaHastaConcesion (YYYYMMDD)
    #   - page, pageSize
    # paginación server-side hasta count, tope 200
    # → mapear JSON → [
    #   {nif, beneficiario, organo, importe, fechaConcesion, titulo, finalidad, url}
    # ]
```

### Riesgo

La API pública de BDNS se descubrió tras verificar; si el esquema de parámetros JSON cambia, el `main.py` logea la URL real y devuelve `SEARCH_FAILED` con detalle. Parámetro de mitigación: parseo defensivo con `.get()`.

## 5. Módulo: `contratacion_search` (completar)

### URL base / feeds verificados

- Feed en vivo: `https://contrataciondelsectorpublico.gob.es/sindicacion/sindicacion_643/licitacionesPerfilesContratanteCompleto3.atom`
- Zips anuales: `.../licitacionesPerfilesContratanteCompleto3_{YYYY}.zip` (2012–presente)
- Formato: CODICE/UBL XML con namespaces:
  - `atom`, `cbc`, `cac`, `cac-place-ext`, `cbc-place-ext`

### `tool.yaml`

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

### `main.py` — lógica híbrida

```python
import requests, zipfile, tempfile, shutil
import xml.etree.ElementTree as ET

NS = {
  "atom": "http://www.w3.org/2005/Atom",
  "cbc": "urn:dgpe:names:draft:codice:schema:xsd:CommonBasicComponents-2",
  "cac": "urn:dgpe:names:draft:codice:schema:xsd:CommonAggregateComponents-2",
  "cac-place-ext": "urn:dgpe:names:draft:codice-place-ext:schema:xsd:CommonAggregateComponents-2",
  "cbc-place-ext": "urn:dgpe:names:draft:codice-place-ext:schema:xsd:CommonBasicComponents-2",
}
LIVE_FEED = "https://contrataciondelsectorpublico.gob.es/sindicacion/sindicacion_643/licitacionesPerfilesContratanteCompleto3.atom"
ZIP_BASE = "https://contrataciondelsectorpublico.gob.es/sindicacion/sindicacion_643/licitacionesPerfilesContratanteCompleto3"

def search_contratacion(target, target_type, date_from, date_to, count):
    # 1. calcular days = (date_to - date_from).days
    # 2. if days <= 90: _search_live_atom(...)
    #    else: _search_zip_files(...)
    # tope páginas: 20 | tope años: 5 | RANGE_TOO_WIDE si >5 años

def _search_live_atom(target, target_type, date_from, date_to, count):
    # GET LIVE_FEED, iterar entries, seguir link[rel=next], parsear CODICE,
    # filtrar por NIF/razón en TenderResult/WinningParty, tope 20 páginas

def _search_zip_files(target, target_type, date_from, date_to, count):
    # para cada año: GET {ZIP_BASE}_{year}.zip (stream=True),
    # unzip en tempfile.mkdtemp(), parsear cada *.atom con _parse_entries,
    # shutil.rmtree en finally. tope 5 años.
```

### Campos devueltos (uniformes)

`id_licitacion`, `organo_contratacion`, `estado`, `importe_estimado`, `fecha_publicacion`, `adjudicatario_nif`, `adjudicatario_nombre`, `importe_adjudicacion`, `fecha_adjudicacion`, `url_detalle`.

### Riesgos

Ver Tabla en el diseño original (Sección 2). Clave: streaming XML para feeds >5MB; parseo defensivo con `ET.fromstring` + try/except; escape de caracteres especiales.

## 6. Módulo: `doue_search` (nuevo)

### WSDL verificado

- Endpoint SOAP: `https://eur-lex.europa.eu/EURLexWebService` (HTTP 200)
- Operación: `GetNoticeList` (SOAPAction `http://eur-lex.europa.eu/search/GetNoticeList`)
- WSDL: 56 líneas, pública.

### `tool.yaml`

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

### `main.py` — SOAP manual con requests

Decisión: construir envelope SOAP a mano (no `zeep`, que no es dependencia del repo).

```python
import requests, xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

SOAP_NS = "http://eur-lex.europa.eu/search"
WSDL_URL = "https://eur-lex.europa.eu/EURLexWebService"

def search_doue(target, target_type, date_from, date_to, count):
    envelope = _build_envelope(target, date_from, date_to, count)
    resp = requests.post(
        WSDL_URL,
        data=envelope,
        headers={"Content-Type":"text/xml; charset=utf-8",
                 "SOAPAction":"http://eur-lex.europa.eu/search/GetNoticeList"},
        timeout=60,
    )
    root = ET.fromstring(resp.content)
    return _parse_notices(root, target)

def _build_envelope(query, dd_desde, dd_hasta, max_results):
    # query = f'DN=(" {escape(target)} ") AND DD >= {dd_desde} AND DD <= {dd_hasta}'
    # resultScope=FULL, maxResults=min(count,100)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <GetNoticeList xmlns="http://eur-lex.europa.eu/search">
      <query>{escape(query)}</query>
      <resultScope>FULL</resultScope>
      <maxResults>{min(count,100)}</maxResults>
    </GetNoticeList>
  </soap:Body>
</soap:Envelope>"""
```

### Campos devueltos

`celex`, `title`, `document_type`, `date_publication`, `oj_reference`, `url_eurlex`, `matched_text`.

### Riesgos

Ver tabla original (Sección 3). Mitigación clave: `xml.sax.saxutils.escape` sobre el query; parseo defensivo con namespaces.

## 7. Módulo: `transparency_search` (nuevo)

### URLs verificadas

- Buscador publicidad activa: `https://www.transparencia.gob.es/servicios-buscador/buscar.htm`
- Altos Cargos / Currículums: `/publicidad-activa/por-materias/altos-cargos/curriculos`
- Altos Cargos / Actividad privada tras cese: `/publicidad-activa/por-materias/altos-cargos/actividad-privada-cese`

### `tool.yaml`

```yaml
name: transparency_search
description: "Busca en Portal de Transparencia: publicidad activa y Altos Cargos."
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

### `main.py` — scraping stdlib

Decisiones:
- NO `BeautifulSoup` (no es dependencia del repo); usar `html.parser` stdlib.
- User-Agent realista, 1 request/página, 10 páginas máximo.

```python
import requests
from html.parser import HTMLParser

PA_URL = "https://www.transparencia.gob.es/servicios-buscador/buscar.htm"

def search_transparency(target, target_type, scope, date_from, date_to, count):
    results = []
    if scope in ("all","publicidad_activa"):
        results += _search_publicidad_activa(target, date_from, date_to, count)
    if scope in ("all","altos_cargos") and len(results) < count:
        if target_type == "name":
            results += _search_altos_cargos(target, date_from, date_to, count - len(results))
        else:
            logger.warning("Altos Cargos no soporta búsqueda por NIF/CIF; se omite.")
    return results[:count], None
```

### Riesgos

Ver tabla del diseño original (Sección 3). Mitigación clave: selectores CSS semánticos por `id`/`class`; si un selector falla, devolver `[]` con log `PARSE_WARNING` y HTML en stderr (NO regex fallback — ver sección 13.6); GET→POST dual-step si el buscador requiere token CSRF.

## 8. Orquestador y toolsets

`internal/orchestrator/investigate.go` itera sobre `o.cfg.Tools` (post-filtrado por toolset activo) y ejecuta en paralelo todos los tools cuyo nombre termina en `_search`. **`defaultSources` deja de estar hardcoded** (línea 44-46) y se deriva dinámicamente. Los 4 módulos nuevos entran al registrarlos en `toolsets.yaml` + al discovery automático — no hay que tocar Go para añadirlos, SOLO para el cambio de `defaultSources` que se menciona arriba.

### `configs/toolsets.yaml` (diff)

```diff
  osint-es:
    tools:
      ...
      - bdns_search
      - contratacion_search
+     - doue_search
+     - transparency_search
      - searxng_search
```

## 9. Tests

### Smoke test Python (runtime, no infra nueva)

```sh
echo '{"request_id":"t","arguments":{"target":"Banco Santander","target_type":"name","date_from":"2025-01-01","date_to":"2026-01-01"}}' | python3 tools/bdns_search/main.py
```

Cada `main.py` incluye `if __name__ == "__main__"` con smoke test echo.

### Tests Go (`internal/orchestrator/investigate_test.go`)

El test actual (`Investigate_empty` y `Investigate_noSources`, 47 líneas totales) comprueba el **edge case de cero tools**. Hay que **reescribirlo** (no ampliarlo) porque el orquestador ya no hardcodea `defaultSources`.

Test de reescritura `TestInvestigateFanOutAllSearchTools`:
- Construye un `executor.Executor` mockeado via `NewMockExecutor(map[toolName]mcptypes.SubprocessResponse)`.
- Configura `o.cfg.Tools` con las 8 tools `*_search` del toolset `osint-es` (excluye `journalist_investigate` que es el orquestador): `ted_search, borme_search, boe_search, bdns_search, contratacion_search, doue_search, transparency_search, searxng_search`.
- Llamada: `Investigate(ctx, {target:"Banco Santander", date_from:"2025-01-01", date_to:"2026-01-01"})`.
- Aserciones:
  - `len(result.SourceResults) == 8` (fan-out paralelo a todas).
  - Cada `SourceResult.Source` está en el set esperado.
  - Los argumentos propagados a cada tool incluyen `target`, `date_from`, `date_to` (verificado vía mock que registra args).
  - `result.TotalResults` cuenta solo los `Success`.
- Order no garantizado (mutex en el bucle del orquestador).

NOTA: el test actual verifica `len(SourceResults) == 0` con tools vacíos. Ese edge case se preserva como `TestInvestigate_emptySources`, separado del test de fan-out.

### Plan de verificación (checklist)

1. `go vet ./...` — sin errores.
2. `go test ./internal/orchestrator/... -run TestInvestigate -v` — pasa, 9 tools fan-out.
3. `go test ./internal/config/...` — discover 4 `tool.yaml` nuevos sin validación.
4. `go build ./cmd/server/` — compila.
5. Smoke test manual de cada `main.py` (ver sección 9.1).
6. `tools/list` devuelve 4 tools nuevos (bdns_search funcionando, contratacion_search funcionando, doue_search, transparency_search).
7. `journalist_investigate {target:"Banco Santander",date_from:"2025-01-01",date_to:"2026-01-01"}` → 9 sub-resultados.

## 10. Esfuerzo estimado

| Módulo | Líneas | Complejidad |
|---|---|---|
| `bdns_search` | ~150 | Media |
| `contratacion_search` | ~250 | Alta |
| `doue_search` | ~200 | Media-alta |
| `transparency_search` | ~220 | Alta |
| Módulo | Líneas | Complejidad |
|---|---|---|
| `bdns_search` | ~150 | Media |
| `contratacion_search` | ~250 | Alta |
| `doue_search` | ~200 | Media-alta |
| `transparency_search` | ~220 | Alta |
| `tools/common/evidence.py` | ~60 | Baja |
| `tools/common/http.py` | ~80 | Baja |
| `tools/common/entity_normalizer.py` | ~40 | Baja |
| Migración 4 tools existentes a `common/http.py` | ~60 | Media |
| `configs/toolsets.yaml` | +2 líneas | Baja |
| `internal/orchestrator/investigate.go` | ~15 | Baja |
| `internal/orchestrator/investigate_test.go` | ~120 (reescritura) | Media |
| **Total** | ~1275 | |

## 11. Riesgos globales y mitigación

| Riesgo | Mitigación |
|---|---|
| APIs/scrapings fallan en runtime | Cada tool error estructurado `SEARCH_FAILED`; orquestador agrega errores parciales sin abortar |
| Timeout total orquestador (10s default/tool) | timeouts por tool en `tool.yaml` (90–120s) |
| Dependencias Python ausentes | Solo `requests` (repo). `xml`, `html`, `zipfile`, `tempfile` = stdlib |
| Scope creep | 4 módulos aislados; uno falla no rompe los demás |
| Regresión en tools existentes por migración a `common/http.py` | Smoke test manual de boe/borme/ted/searxng tras migración, ANTES de tocar los nuevos; test de integración Go existente como semáforo |
| Divergencia de esquema en `common/http.py` vs `requests` directo | La migración usa `request_with_retry` como drop-in: misma signature (url, params, headers, timeout) → cero cambios en lógica de parsing

## 12. Aprobación → transición a implementación

Tras aprobación y revisión de este spec:
- Invocar `writing-plans` para el plan de implementación (SDD tasks).
- **Sí se requiere cambio en Go**: ~15 líneas en `investigate.go` para derivar `defaultSources` dinámicamente, y reescritura del test de fan-out (~120 líneas).

---

## 13. Endurecimiento transversal (revisiones técnicas incorporadas)

> Basado en el plan de endurecimiento recibido como feedback técnico. Esta sección documenta decisiones aprobadas y rechazos con justificación técnica para futuros maintainers.

### 13.1 Modelo de evidencia común — ACEPTADO (simplificado)

Todas las tools devuelven dentro de `structured_content.results[]` items con esta forma mínima:

```json
{
  "id": "sha256_hexdigest_de_url+timestamp+title[:16]",
  "source": "BDNS",
  "official": true,
  "confidence": 1.0,
  "title": "Subvención X a empresa Y",
  "date": "2025-06-15",
  "url": "https://...",
  "metadata": { ... campos específicos de la fuente ... }
}
```

Rationale: el orquestador ya define `SourceResult.Data map[string]interface{}` (investigate.go:25) y cada tool devuelve `{source, target, results, count}`. El modelo `Evidence` **estandariza los items dentro de `results[]`**, no reemplaza el contrato. Helper: `tools/common/evidence.py` (~60 líneas).

NO se incluye `raw` (chico, no mejora al LLM) ni `summary` (lo genera el LLM).

### 13.2 Metadatos comunes — ACEPTADO

Cada `structured_content` incluye:
```json
{ "official": true, "confidence": 1.0, "retrieved_at": "2026-08-03T...", "query": "Banco Santander" }
```
Coste: 4 líneas por tool.

### 13.3 Identificador de evidencia — ACEPTADO (hash determinista)

`id = sha256(url + publication_date + title)[:16]` — fuente-agnóstico, permite dedup. NO usar IDs tipo `BDNS-2025-12` porque exigen conocer el key de cada fuente.

### 13.4 Normalizador de entidades — ACEPTADO (solo matching)

`tools/common/entity_normalizer.py` (`normalize_for_match`: lowercase, strip accents, strip legal forms `S.A., S.L., S.L.U.`). Usado solo para **comparar** `target` contra campos devueltos; el display conserva el nombre original de la fuente. Importante en `transparency_search` y `contratacion_search` donde el matching es por texto.

### 13.5 Capa HTTP compartida — ACEPTADO (mínima) y MIGRADA a tools existentes

`tools/common/http.py`:
- `get_session()` → `requests.Session` con pool de conexiones.
- `request_with_retry(method, url, params=None, json=None, headers=None, timeout=30)` → retry exponencial (3 intentos), respeta `Retry-After` en 429/503, logea intentos via `structured_logging`.

**Coherencia total**: los 4 tools existentes (`boe_search`, `borme_search`, `ted_search`, `searxng_search`) migran a esta utilidad — reemplazar `requests.get(url, params, timeout, headers)` por `http.request_with_retry("GET", url, params=..., headers=..., timeout=...)`. Drop-in signature, cero cambios en parsing.

### 13.6 Rechazos con justificación

| # | Propuesta | Decisión | Justificación técnica |
|---|---|---|---|
| 1 | **Caché compartida** (cache/hash(query).json) | RECHAZADO | El executor corre cada tool como subprocess efímero (`subprocess.go:191`). Un cache en disco introduce estado mutable en un proceso sin estado, invalidación por TTL no trivial y no hay proceso persistente donde cachear. Además YAGNI: fuentes oficiales no se repiten en una sesión de investigación. Posponer a iteración 2 con datos reales de uso. |
| 2 | **Capas separadas en `contratacion_search`** (Downloader→Parser→Mapper→Output, 4 archivos) | RECHAZADO | Over-engineering para ~250 líneas. Reemplazo: una clase `ContratacionSearcher` con 4 métodos privados `_download_live/_download_zip/_parse_entry/_to_evidence`. Un archivo, legible y testeable. Se extrae a múltiples archivos SOLO si la clase crece >400 líneas. |
| 3 | **Regex fallback para parsing de Transparencia** | RECHAZADO | Antipatrón: regex sobre HTML se pudre silenciosamente al cambiar la estructura. Reemplazo: usar `html.parser` con IDs/clases semánticos; si un selector falla, devolver `[]` con log `PARSE_WARNING` y HTML guardado en stderr para depuración. Regex no forma parte de la arquitectura. |

### 13.7 Riesgo semilla de esta sección

Un maintainer futuro podría reintroducir `zeep` o `BeautifulSoup` en los nuevos tools. La regla queda documentada: dependencia única aprobada es `requests`. Todo lo demás usa stdlib (`xml.etree`, `html.parser`, `zipfile`, `hashlib`, `xml.sax.saxutils`).
