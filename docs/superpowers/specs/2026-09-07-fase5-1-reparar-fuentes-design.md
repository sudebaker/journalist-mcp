# Diseño: Fase 5.1 — Reparar fuentes BOE/BORME (`boe_search`, `borme_search`)

## 1. Contexto

`journalist_investigate` agrega en paralelo las fuentes `*_search` del toolset. Tras verificar en vivo la API de datos abiertos del BOE/BORME se descubrió que **`boe_search` y `borme_search` están rotos en producción**: ambos parsean `data["item"]`, pero la estructura real del JSON es anidada, por lo que **siempre devuelven `count: 0`** (confirmado ejecutando `boe_search` con "Banco Santander" sobre un rango real → 0 resultados). Los tests unitarios no lo detectaron porque mockean el fetch.

Este spec repara ambos tools, documenta sus limitaciones reales y deja en backlog dos fuentes que se descartaron tras verificación (concursos RPC y registro de perfiles de contratante PLACSP).

## 2. Alcance

| Módulo | Tipo de cambio | Descripción |
|---|---|---|
| `tools/boe_search/main.py` | Fix mecánico | Parsear jerarquía real del sumario BOE; match por `titulo`; URL canónica. |
| `tools/boe_search/tool.yaml` | Sin cambios | Misma interfaz `{target, target_type, date_from, date_to}`. |
| `tests/tools/test_boe_search.py` | Reescritura | Fixtures de la estructura JSON real anidada. |
| `tools/borme_search/main.py` | Fix real | Parsear jerarquía real; Sección Primera vía XML por provincia; Sección Segunda por título directo; **solo búsqueda por nombre**. |
| `tools/borme_search/tool.yaml` | Ajuste de descripción | Documentar "solo por nombre" y el nuevo default configurable. |
| `tests/tools/test_borme_search.py` | Reescritura | Fixtures reales + parser XML BORME + rechazo `nif/cif`. |

Fuera de alcance (backlog, ver §7): concursos/insolvencias (RPC sin API), registro de perfiles de contratante PLACSP (feed con certificado), `altos_cargos`.

## 3. Hallazgos verificados (evidencia)

### 3.1 Estructura real del sumario BOE

`GET https://www.boe.es/datosabiertos/api/boe/sumario/{aaaammdd}` con header `Accept: application/json` → HTTP 200.

```json
{
  "status": {"code": "200"},
  "data": {
    "sumario": {
      "metadatos": {"publicacion": "BOE", "fecha_publicacion": "20260904"},
      "diario": [{
        "numero": "219",
        "seccion": [{
          "codigo": "2A",
          "nombre": "II. Autoridades y personal. - A. Nombramientos...",
          "departamento": [{
            "codigo": "1820",
            "nombre": "CONSEJO GENERAL DEL PODER JUDICIAL",
            "epigrafe": [{
              "nombre": "Situaciones",
              "item": [{
                "identificador": "BOE-A-2026-18582",
                "control": "2026/11642",
                "titulo": "Acuerdo de 24 de marzo de 2026...",
                "url_pdf": {"texto": "https://www.boe.es/boe/dias/2026/09/04/pdfs/BOE-A-2026-18582.pdf"},
                "url_html": "https://www.boe.es/diario_boe/txt.php?id=BOE-A-2026-18582",
                "url_xml": "https://www.boe.es/diario_boe/xml.php?id=BOE-A-2026-18582"
              }]
            }]
          }]
        }]
      }]
    }
  }
}
```

- Ruta real de los items: `data.sumario.diario[].seccion[].departamento[].epigrafe[].item[]`.
- Campos del item: `identificador`, `control`, `titulo`, `url_pdf.texto`, `url_html`, `url_xml`. **No existe campo `contenido`** en el sumario: el match solo puede usar `titulo` (y `identificador`/`control` como metadatos). El texto completo exige descargar `url_xml`/`url_html` por item, lo cual se descarta por coste.
- Secciones observadas: `1`, `2A`, `2B`, `3`, `4` (Administración de Justicia), `5A` (Anuncios A), `5B` (Anuncios B). **Las secciones `4`, `5A`, `5B` llegan con 0 items en varios días muestreados** → el sumario API no expone los anuncios. La palabra "concurso" en títulos corresponde a "oposiciones y concursos" (empleo público), no a concursos de acreedores.
- El endpoint rechaza el request sin el header `Accept: application/json` (400).

### 3.2 Estructura real del sumario BORME

`GET https://www.boe.es/datosabiertos/api/borme/sumario/{aaaammdd}` con `Accept: application/json` → HTTP 200.

```json
{
  "status": {"code": "200"},
  "data": {
    "sumario": {
      "diario": [{
        "numero": "170",
        "seccion": [
          {
            "codigo": "A",
            "nombre": "SECCIÓN PRIMERA. Empresarios. Actos inscritos",
            "item": [{
              "identificador": "BORME-A-2026-170-01",
              "titulo": "ARABA/ÁLAVA",   // ← la provincia, NO la empresa
              "url_pdf": {"texto": "..."},
              "url_html": "https://www.boe.es/diario_borme/txt.php?id=BORME-A-2026-170-01",
              "url_xml": "https://www.boe.es/diario_borme/xml.php?id=BORME-A-2026-170-01"
            }]
          },
          {
            "codigo": "C",
            "nombre": "SECCIÓN SEGUNDA. Anuncios y avisos legales",
            "apartado": [{
              "codigo": "002",
              "nombre": "CONVOCATORIAS DE JUNTAS",
              "item": [{
                "identificador": "BORME-C-2026-4855",
                "titulo": "CONTRATAS Y OBRAS SAN GREGORIO, S.A.",  // ← la entidad, directo
                "url_html": "...", "url_xml": "..."
              }]
            }]
          }
        ]
      }]
    }
  }
}
```

- Ruta real: `data.sumario.diario[].seccion[]` donde cada sección tiene `item[]` (Sección Primera, código `A`) o `apartado[].item[]` (Sección Segunda, código `C`).
- **Sección Primera**: el `titulo` del item es la **provincia/registro** (ej. "ARABA/ÁLAVA"). Los nombres de empresa y actos están en el documento XML de ese item (`url_xml`).
- **Sección Segunda**: el `titulo` del item **es la entidad** (ej. "CONTRATAS Y OBRAS SAN GREGORIO, S.A."). Match directo sobre el sumario, sin descargar XML.

### 3.3 Estructura del XML BORME (por item de Sección Primera)

`GET https://www.boe.es/diario_borme/xml.php?id=BORME-A-2026-170-01` → HTTP 200, XML:

```xml
<documento fecha_actualizacion="20260903T071717Z">
  <metadatos>
    <identificador>BORME-A-2026-170-01</identificador>
    <titulo>ARABA/ÁLAVA</titulo>
    <diario>Boletín Oficial del Registro Mercantil</diario>
    <seccion codigo="A">SECCIÓN PRIMERA. Empresarios. Actos inscritos</seccion>
    <fecha_publicacion>20260903</fecha_publicacion>
  </metadatos>
  <texto>
    <p class="articulo">401698 - QUALIS CONSULTORES DE TALENTO SOCIEDAD LIMITADA.</p>
    <p class="parrafo">Declaración de unipersonalidad. Socio único: TRISKELION INVE...</p>
    <p class="articulo">401699 - SEÑALIZACION Y BALIZAMIENTOS JUNDIZ, SOCIEDAD LIMITADA.</p>
    <p class="parrafo">Sociedad unipersonal. Cambio de identidad del socio único: E...</p>
  </texto>
</documento>
```

- Cada empresa es un `<p class="articulo">` con formato `NÚMERO - NOMBRE EMPRESA.` seguido de uno o más `<p class="parrafo">` con sus actos.
- **No se publican NIF/CIF** en el BORME abierto (enmascarados por protección de datos) → la búsqueda por NIF/CIF no es posible; solo por nombre.

### 3.4 `contratacion_search` ya cubre el órgano de contratación

`_entry_matches` en `tools/contratacion_search/main.py` ya matchea:
- `name`: contra `organo_contratacion` **y** `adjudicatario_nombre` (substring bidireccional normalizado).
- `nif/cif`: contra la lista `nifs` del entry (incluye el NIF del órgano vía `_collect_nifs`).

No se necesita un tool nuevo de "perfiles de contratante" para buscar por órgano de contratación.

## 4. Módulo 1 — `boe_search` (fix mecánico)

### 4.1 Cambios en `main.py`

1. Reemplazar `items = data.get("item", [])` por un iterador que aplane la jerarquía:
   `data["sumario"]["diario"] → seccion → departamento → epigrafe → item`, devolviendo `(item, seccion_codigo, epigrafe_nombre)`.

2. Construir la URL canónica del item:
   - `url_html` si existe; si no, `url_pdf["texto"]`.

3. `matches_target` se mantiene (mismo criterio sobre `titulo`), pero ahora opera sobre el dict del item real:
   - `name`: substring case-insensitive sobre `titulo`.
   - `nif`/`cif`: substring uppercase sobre `titulo`.
   - El match sobre "contenido" desaparece porque el sumario no lo trae (documentado en la descripción del tool como limitación).

4. `build_evidence` igual que hoy, con:
   - `source="boe"`, `official=True`, `confidence=0.9`
   - `title=titulo`, `date` = fecha del día (`%Y-%m-%d`), `url` = url canónica
   - `metadata={"identificador": ..., "seccion": ..., "epigrafe": ...}`

5. Se añade `control` de peticiones: el header `Accept: application/json` ya se envía (imprescindible, §3.1).

### 4.2 Limitación documentada (en `tool.yaml` description y README del tool)

El sumario de datos abiertos **no expone la Sección V (Anuncios) ni items de Administración de Justicia**: solo disposiciones (I), autoridades/personal (II) y otras disposiciones (III). `boe_search` encuentra menciones del target en títulos de disposiciones/nombramientos/oposiciones, **no** anuncios de licitaciones/concursos.

## 5. Módulo 2 — `borme_search` (fix real, solo nombre)

### 5.1 Interfaz (`tool.yaml`)

```yaml
name: borme_search
description: "Busca actos mercantiles en BORME por NOMBRE de empresa (el BORME abierto no publica NIF/CIF). Descarga sumarios por rango y extrae actos desde los XML por provincia."
command: python3
args: [main.py]
timeout: 300s
input_schema:
  type: object
  properties:
    target: {type: string, description: "Nombre de empresa a buscar"}
    target_type:
      type: string
      enum: [nif, cif, name]
      default: name
      description: "Solo 'name' está soportado; 'nif'/'cif' devuelven SEARCH_FAILED (BORME no publica NIF)."
    date_from: {type: string, description: "YYYY-MM-DD. Default: BORME_LOOKBACK_DAYS días atrás."}
    date_to: {type: string, description: "YYYY-MM-DD. Default: hoy."}
    count: {type: integer, default: 20}
  required: [target]
```

- `target_type` default pasa de `nif` a `name`.
- `nif`/`cif` explícitos → respuesta `SEARCH_FAILED` con mensaje claro:
  `"BORME no publica NIF/CIF (enmascarados por protección de datos); busque por nombre de empresa."`

### 5.2 Lógica (`main.py`)

1. **Lookback configurable**: nueva constante `DEFAULT_LOOKBACK_DAYS = int(os.environ.get("BORME_LOOKBACK_DAYS", "7"))`. Rango máximo 90 días (como hoy) → `SEARCH_FAILED` si se excede.

2. **Parsear el sumario** del día: recorrer `data.sumario.diario[].seccion[]`:
   - Sección `A` (Primera): acumular items (provincia + `url_xml`) para la fase 3.
   - Sección `C` (Segunda): matchear `item.titulo` directamente (ver fase 4).

3. **Sección Primera — XML por provincia** (solo para los días del rango):
   - Para cada item, `GET url_xml` con `request_with_retry` (timeout 15s).
   - Parsear con `xml.etree.ElementTree`: dentro de `metadatos` leer `fecha_publicacion`; dentro de `texto`, iterar los `<p>` y agrupar por `class="articulo"` (inicio de empresa) con sus `<p class="parrafo">` siguientes.
   - Nombre de empresa = texto del `articulo` sin el prefijo `NÚMERO - ` y sin el punto final.
   - Match por nombre normalizado (`normalize_for_match`): `target_norm in name_norm or name_norm in target_norm` (igual criterio bidireccional de `contratacion_search`).
   - Cada empresa que matchea → un evidence:
     - `source="borme"`, `official=True`, `confidence=0.9`
     - `title` = nombre de empresa (sin el número)
     - `date` = `fecha_publicacion` (formato `%Y-%m-%d`)
     - `url` = `url_html` del item de provincia
     - `metadata={"identificador", "provincia" (titulo del item), "actos": [textos parrafo], "seccion": "A"}`
   - Cortar al alcanzar `count`.
   - Errores por item (red/parse) → `logger.warning` y continuar con el siguiente (no abortar el día).

4. **Sección Segunda — match directo en sumario**:
   - Para cada `item` de cada `apartado`, matchear `item.titulo` por nombre normalizado.
   - Cada match → evidence con `title` = título del item, `url` = `url_html`, `metadata={"identificador", "apartado", "seccion": "C"}`. Sin descarga de XML.

5. **Coste acotado**: la fase 3 domina (~50 provincias/día máximo, pero solo Sección Primera). Mitigación:
   - **Descarga concurrente**: los `GET url_xml` de provincia se ejecutan con `ThreadPoolExecutor(max_workers=8)` (stdlib); los resultados se ordenan de forma determinista tras la recolección para no depender del orden de finalización.
   - Cortar al alcanzar `count` (no lanzar más descargas una vez satisfecho).
   - Default de solo 7 días (`BORME_LOOKBACK_DAYS`).
   - `timeout: 300s` en `tool.yaml` (§5.1) porque el peor caso (sin matches) exige barrer el rango completo.

### 5.3 Nota sobre concursos

Los "actos inscritos" de Sección Primera incluyen actos concursales (p. ej. "nombramiento de administrador concursal") como texto de `<p class="parrafo">`. La reparación de `borme_search` recupera **parcialmente** la señal de concursos sin fuente dedicada. No se añade filtro específico en este spec (sería ruido sobre el caso general); se documenta como posible evolución.

## 6. Tests

### 6.1 `tests/tools/test_boe_search.py` (reescritura)

- Fixture del JSON de sumario BOE real anidado (ver §3.1), embebido como dict.
- `fetch_boe_day` mockeado para devolver la estructura cruda; aserción de que `iter_boe_items` aplana correctamente (2 departamentos × epigrafes × items).
- `search_boe("X", "name", ...)` sobre el fixture → devuelve solo items cuyo `titulo` contiene "X" (case-insensitive).
- `search_boe("B12345678", "nif", ...)` → solo items cuyo `titulo` contiene el NIF en mayúsculas.
- URL canónica = `url_html`; fallback `url_pdf.texto` cuando no hay `url_html`.
- El evidence generado tiene los campos del contrato (`id`, `source="boe"`, `date`, `url`).

### 6.2 `tests/tools/test_borme_search.py` (reescritura)

- Fixture del JSON de sumario BORME real (sección `A` con items por provincia + sección `C` con apartados e items por entidad).
- Fixture del XML BORME por provincia (ver §3.3) como texto.
- Parser XML: extrae pares `(nombre_empresa, actos[])` correctamente (quita prefijo numérico y punto final; agrupa parrafos tras cada articulo).
- `search_borme("QUALIS CONSULTORES", "name", ...)` → 1 evidence de Sección Primera con actos no vacíos; title sin el número.
- Match de Sección Segunda: `search_borme("SAN GREGORIO", ...)` → 1 evidence con `seccion="C"`, sin descarga de XML.
- `search_borme("B12345678", "nif", ...)` → error claro.
- Lookback default desde env: monkeypatch `BORME_LOOKBACK_DAYS` y verificar rango generado.

### 6.3 Verificación final (manual, no CI)

```sh
echo '{"request_id":"t","arguments":{"target":"Banco Santander","target_type":"name","date_from":"2026-08-01","date_to":"2026-08-31"}}' | python3 tools/boe_search/main.py
echo '{"request_id":"t","arguments":{"target":"Santander","target_type":"name","date_from":"2026-08-01","date_to":"2026-08-31"}}' | python3 tools/borme_search/main.py
```

Ambos deben devolver `count > 0` (o `0` legítimo con texto "No se encontraron resultados" cuando el target no aparece, nunca por un bug de parsing).

## 7. Backlog documentado

| Fuente | Motivo | Estado |
|---|---|---|
| Concursos/insolvencias (RPC) | `publicidadconcursal.es` es portal Liferay sin API abierta; scraping frágil y potencial ToS | Backlog |
| Registro de perfiles de contratante PLACSP | Feed `PerfilesContratanteCompleto3.atom` exige certificado | Backlog |
| `transparency_search` altos_cargos | Portal sin endpoint buscable | Backlog (previo) |

## 8. Esfuerzo estimado

| Módulo | Líneas aprox. | Complejidad |
|---|---|---|
| `boe_search` fix (`iter_boe_items` + URL canónica) | ~40 | Baja |
| `borme_search` fix (sumario + XML por provincia + agrupación) | ~150 | Media |
| Tests `boe_search` | ~80 (reescritura) | Media |
| Tests `borme_search` | ~120 (reescritura) | Media-alta |
| **Total** | ~390 | |

## 9. Riesgos y mitigación

| Riesgo | Mitigación |
|---|---|
| BOE/BORME cambian la estructura JSON | Parseo defensivo (`.get()`); tests con fixture actual; si el XML/JSON cambia, el tool devuelve `SEARCH_FAILED` con detalle, nunca resultados falsos |
| Coste de descargar ~50 XMLs de provincia/día en BORME | Default 7 días (`BORME_LOOKBACK_DAYS`), cortar al alcanzar `count`, continuar ante errores por item |
| Match por nombre genera falsos positivos (empresas homónimas) | Normalización + substring bidireccional; `confidence=0.9`; el LLM decide con el contexto |
| `boe_search` con valor limitado (sin anuncios en la API) | Documentado en `tool.yaml` y en este spec; el fix es de corrección, no de ampliación |
