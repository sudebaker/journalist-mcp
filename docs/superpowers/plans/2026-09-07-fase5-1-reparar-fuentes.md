# Fase 5.1 — Reparar fuentes BOE/BORME Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reparar `boe_search` y `borme_search` para que parseen la estructura JSON/XML real de las APIs de datos abiertos (hoy devuelven `count: 0` siempre), reescribiendo sus tests con fixtures reales.

**Architecture:** Ambos tools son subprocess Python con contrato JSON stdin/stdout. El fix no toca Go ni el contrato de evidencia: cambia solo el parsing del sumario (anidado) y, en BORME, añade descarga+parseo del XML por provincia con concurrencia acotada. Se reutilizan `common.http.request_with_retry`, `common.evidence.build_evidence/build_search_result` y `common.entity_normalizer.normalize_for_match`.

**Tech Stack:** Python 3 stdlib (`json`, `xml.etree.ElementTree`, `concurrent.futures`, `re`, `os`, `datetime`), `requests` vía `common.http`. Sin dependencias nuevas.

**Spec:** `docs/superpowers/specs/2026-09-07-fase5-1-reparar-fuentes-design.md`

## Global Constraints

- Dependencia HTTP única: `from common.http import request_with_retry, REQUESTS_AVAILABLE`. Nunca `requests.get/post` directos.
- `stdout` reservado para el JSON del protocolo MCP. Todo log va a stderr vía `common.structured_logging.get_logger`.
- Evidencia construida con `common.evidence.build_evidence` / `build_search_result`. `source="boe"` / `source="borme"`, `official=True`, `confidence=0.9`.
- Los tests son **offline**: mockean `fetch_boe_day`/`fetch_borme_day` y `fetch_province_xml`. Nunca abren red.
- Comandos de verificación: `python3 -m pytest tests/tools/test_boe_search.py tests/tools/test_borme_search.py -q` (rápido), y `python3 -m pytest tests/ -q` (suite completa, ~26s).
- Commits: conventional commits en inglés, sin firma de IA, sin Co-Authored-By.
- No añadir dependencias a `deployments/requirements.txt`.
- La fecha de evidencia se emite en formato ISO `%Y-%m-%d` (consistente con el resto de tools).

---

### Task 1: `boe_search` — aplanar el sumario BOE y matchear por título

**Files:**
- Modify: `tools/boe_search/main.py`
- Test: `tests/tools/test_boe_search.py` (reescritura)

**Interfaces:**
- Consumes: `common.http.request_with_retry`, `common.evidence.build_evidence/build_search_result`, `common.structured_logging.get_logger` (ya importados).
- Produces:
  - `iter_boe_items(data: dict) -> list[dict]` — aplanado de items enriquecidos con claves `seccion` (código) y `epigrafe` (nombre).
  - `canonical_item_url(item: dict) -> str` — `url_html` o fallback `url_pdf["texto"]`.
  - `matches_target(item: dict, target: str, target_type: str) -> bool` — solo sobre `titulo`.
  - `fetch_boe_day(date_str: str) -> list[dict]` — GET del sumario del día + aplanado.
  - `search_boe(target, target_type, date_from, date_to) -> tuple[list[dict] | None, str | None]`.

- [ ] **Step 1: Reescribir el test con fixture del JSON real**

Reemplaza el contenido de `tests/tools/test_boe_search.py` por:

```python
#!/usr/bin/env python3
"""Offline unit tests for tools/boe_search/main.py — real nested sumario.

No network access: fixtures mirror the real API response shape
(data.sumario.diario[].seccion[].departamento[].epigrafe[].item[]).

Standalone runner: ``python3 tests/tools/test_boe_search.py``
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))

from boe_search import main as boe  # noqa: E402

# Fixture: estructura real observada en https://www.boe.es/datosabiertos/api/boe/sumario/{aaaammdd}
REAL_SUMARIO = {
    "status": {"code": "200", "text": "ok"},
    "data": {
        "sumario": {
            "metadatos": {"publicacion": "BOE", "fecha_publicacion": "20260903"},
            "diario": [
                {
                    "numero": "217",
                    "seccion": [
                        {
                            "codigo": "1",
                            "nombre": "I. Disposiciones generales",
                            "departamento": [
                                {
                                    "codigo": "9999",
                                    "nombre": "MINISTERIO DE PRUEBA",
                                    "epigrafe": [
                                        {
                                            "nombre": "Leyes Orgánicas",
                                            "item": [
                                                {
                                                    "identificador": "BOE-A-2026-11111",
                                                    "control": "2026/10001",
                                                    "titulo": "Ley 1/2026 de prueba con mención a TEST SL.",
                                                    "url_pdf": {"texto": "https://www.boe.es/boe/dias/2026/09/03/pdfs/BOE-A-2026-11111.pdf"},
                                                    "url_html": "https://www.boe.es/diario_boe/txt.php?id=BOE-A-2026-11111",
                                                    "url_xml": "https://www.boe.es/diario_boe/xml.php?id=BOE-A-2026-11111",
                                                }
                                            ],
                                        }
                                    ],
                                }
                            ],
                        },
                        {
                            "codigo": "2A",
                            "nombre": "II. Autoridades y personal. - A. Nombramientos",
                            "departamento": [
                                {
                                    "codigo": "1820",
                                    "nombre": "CONSEJO GENERAL DEL PODER JUDICIAL",
                                    "epigrafe": [
                                        {
                                            "nombre": "Situaciones",
                                            "item": {
                                                "identificador": "BOE-A-2026-22222",
                                                "titulo": "Acuerdo sobre el NIF B12345678.",
                                                "url_pdf": {"texto": "https://www.boe.es/boe/dias/2026/09/03/pdfs/BOE-A-2026-22222.pdf"},
                                            },
                                        }
                                    ],
                                }
                            ],
                        },
                    ],
                }
            ],
        }
    },
}


def test_date_range_bounds():
    dates = boe.date_range("2025-01-01", "2025-01-03")
    assert dates == ["20250101", "20250102", "20250103"]


def test_iter_boe_items_flattens_nested_sections():
    items = boe.iter_boe_items(REAL_SUMARIO)
    assert len(items) == 2
    assert items[0]["identificador"] == "BOE-A-2026-11111"
    assert items[0]["seccion"] == "1"
    assert items[0]["epigrafe"] == "Leyes Orgánicas"
    assert items[1]["identificador"] == "BOE-A-2026-22222"
    assert items[1]["seccion"] == "2A"
    assert items[1]["epigrafe"] == "Situaciones"


def test_iter_boe_items_tolerates_item_as_dict_and_empty():
    assert boe.iter_boe_items({}) == []
    assert boe.iter_boe_items({"data": {"sumario": {"diario": []}}}) == []


def test_matches_target_titulo_only():
    assert boe.matches_target({"titulo": "Acuerdo con B12345678"}, "b12345678", "nif")
    assert boe.matches_target({"titulo": "Ley mención a Test Sl"}, "test sl", "name")
    assert not boe.matches_target({"titulo": "Otra cosa"}, "B12345678", "nif")
    assert not boe.matches_target({"titulo": ""}, "X", "name")


def test_canonical_url_prefers_html_falls_back_pdf():
    item = {"url_html": "https://html", "url_pdf": {"texto": "https://pdf"}}
    assert boe.canonical_item_url(item) == "https://html"
    assert boe.canonical_item_url({"url_pdf": {"texto": "https://pdf"}}) == "https://pdf"
    assert boe.canonical_item_url({}) == ""


def test_search_boe_builds_contract_evidence_iso_date():
    # fetch devuelve items ya aplanados (como hará fetch_boe_day real)
    items = boe.iter_boe_items(REAL_SUMARIO)
    boe.fetch_boe_day = lambda d: items  # type: ignore[assignment]
    results, err = boe.search_boe("test sl", "name", "2026-09-03", "2026-09-03")
    assert err is None
    assert len(results) == 1
    ev = results[0]
    assert ev["source"] == "boe"
    assert ev["official"] is True
    assert ev["title"] == "Ley 1/2026 de prueba con mención a TEST SL."
    assert ev["date"] == "2026-09-03"
    assert ev["url"] == "https://www.boe.es/diario_boe/txt.php?id=BOE-A-2026-11111"
    assert ev["metadata"]["seccion"] == "1"
    assert ev["metadata"]["epigrafe"] == "Leyes Orgánicas"
    assert len(ev["id"]) == 16


def test_search_boe_nif_matches_uppercase_in_titulo():
    items = boe.iter_boe_items(REAL_SUMARIO)
    boe.fetch_boe_day = lambda d: items  # type: ignore[assignment]
    results, err = boe.search_boe("b12345678", "nif", "2026-09-03", "2026-09-03")
    assert err is None
    assert len(results) == 1
    assert results[0]["metadata"]["identificador"] == "BOE-A-2026-22222"


def test_empty_results_is_success_zero():
    boe.fetch_boe_day = lambda d: []  # type: ignore[assignment]
    results, err = boe.search_boe("NADIE", "name", "2025-01-01", "2025-01-01")
    assert err is None
    assert results == []
    env = boe.build_search_result("boe", "NADIE", results)
    assert env["count"] == 0 and env["results"] == []


def test_duplicate_ids_stable_across_calls():
    ev1 = boe.build_evidence("boe", True, 0.9, "T", "2026-09-03",
                             "https://boe.es/x", entity="B1")
    ev2 = boe.build_evidence("boe", True, 0.9, "T", "2026-09-03",
                             "https://boe.es/x", entity="B1")
    assert ev1["id"] == ev2["id"]


if __name__ == "__main__":
    tests = [
        test_date_range_bounds,
        test_iter_boe_items_flattens_nested_sections,
        test_iter_boe_items_tolerates_item_as_dict_and_empty,
        test_matches_target_titulo_only,
        test_canonical_url_prefers_html_falls_back_pdf,
        test_search_boe_builds_contract_evidence_iso_date,
        test_search_boe_nif_matches_uppercase_in_titulo,
        test_empty_results_is_success_zero,
        test_duplicate_ids_stable_across_calls,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}", file=sys.stderr)
            passed += 1
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"FAIL {t.__name__}: {e}", file=sys.stderr)
            failed += 1
    print(f"\n{passed} passed, {failed} failed", file=sys.stderr)
    sys.exit(1 if failed else 0)
```

- [ ] **Step 2: Ejecutar el test para verificar que FALLA**

Run: `python3 -m pytest tests/tools/test_boe_search.py -q`
Expected: FAIL en `test_iter_boe_items_flattens_nested_sections` con `AttributeError: module 'boe_search.main' has no attribute 'iter_boe_items'`.

- [ ] **Step 3: Implementar los helpers en `tools/boe_search/main.py`**

Añade tras la constante `BOE_API` (no borres `date_range`):

```python
def _as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def iter_boe_items(data: dict) -> list[dict]:
    """Flatten the nested BOE sumario into enriched item dicts.

    Real path: data.sumario.diario[].seccion[].departamento[].epigrafe[].item[].
    Each returned item carries extra keys 'seccion' (section code) and
    'epigrafe' (epigraph name) for evidence metadata.
    """
    out: list[dict] = []
    try:
        diario = data["data"]["sumario"]["diario"]
    except (KeyError, TypeError):
        return out
    for day in _as_list(diario):
        for seccion in _as_list(day.get("seccion")):
            sec_code = seccion.get("codigo", "")
            for dep in _as_list(seccion.get("departamento")):
                for ep in _as_list(dep.get("epigrafe")):
                    for it in _as_list(ep.get("item")):
                        if not isinstance(it, dict):
                            continue
                        enriched = dict(it)
                        enriched["seccion"] = sec_code
                        enriched["epigrafe"] = ep.get("nombre", "")
                        out.append(enriched)
    return out


def canonical_item_url(item: dict) -> str:
    """Return url_html, falling back to url_pdf.texto."""
    url = str(item.get("url_html", "") or "")
    if not url:
        pdf = item.get("url_pdf")
        if isinstance(pdf, dict):
            url = str(pdf.get("texto", "") or "")
    return url
```

Reemplaza `matches_target` (el item real no trae `contenido`; solo se matchea `titulo`):

```python
def matches_target(item: dict, target: str, target_type: str) -> bool:
    titulo = str(item.get("titulo", ""))
    if not titulo:
        return False
    if target_type in ("nif", "cif"):
        return target.upper() in titulo.upper()
    return target.lower() in titulo.lower()
```

Reemplaza `fetch_boe_day`:

```python
def fetch_boe_day(date_str: str) -> list[dict]:
    url = f"{BOE_API}/{date_str}"
    try:
        resp = request_with_retry(
            "GET", url, headers={"Accept": "application/json"}, timeout=15
        )
    except Exception as exc:
        logger.warning(
            "BOE request failed",
            extra_data={"date": date_str, "error": str(exc)},
        )
        return []
    if resp.status_code != 200:
        return []
    try:
        return iter_boe_items(resp.json())
    except Exception as exc:
        logger.warning(
            "BOE parse failed",
            extra_data={"date": date_str, "error": str(exc)},
        )
        return []
```

Reemplaza el cuerpo de `search_boe` (fecha de evidencia en ISO, URL canónica, metadata):

```python
def search_boe(target: str, target_type: str, date_from: str, date_to: str):
    if not REQUESTS_AVAILABLE:
        return None, "requests library not available"
    dates = date_range(date_from, date_to)
    if len(dates) > 90:
        return None, "date range too large (max 90 days)"
    results = []
    for d in dates:
        day_iso = datetime.strptime(d, "%Y%m%d").strftime("%Y-%m-%d")
        for item in fetch_boe_day(d):
            if matches_target(item, target, target_type):
                results.append(build_evidence(
                    source="boe",
                    official=True,
                    confidence=0.9,
                    title=str(item.get("titulo", "")),
                    date=day_iso,
                    url=canonical_item_url(item),
                    metadata={
                        "identificador": item.get("identificador", ""),
                        "seccion": item.get("seccion", ""),
                        "epigrafe": item.get("epigrafe", ""),
                    },
                    entity=target,
                    query=target,
                ))
    return results, None
```

- [ ] **Step 4: Ejecutar el test para verificar que PASA**

Run: `python3 -m pytest tests/tools/test_boe_search.py -q`
Expected: PASS (9 tests).

- [ ] **Step 5: Commit**

```bash
git add tools/boe_search/main.py tests/tools/test_boe_search.py
git commit -m "fix(tools): parse real nested BOE sumario in boe_search"
```

---

### Task 2: `borme_search` — aplanar el sumario, lookback configurable, matching por nombre

**Files:**
- Modify: `tools/borme_search/main.py`
- Test: `tests/tools/test_borme_search.py` (ampliación)

**Interfaces:**
- Consumes: `common.http.request_with_retry`, `common.evidence.build_evidence/build_search_result` (ya importados); nuevo `common.entity_normalizer.normalize_for_match`.
- Produces:
  - `DEFAULT_LOOKBACK_DAYS` — vía `_default_lookback_days()` leyendo `BORME_LOOKBACK_DAYS` (default `"7"`).
  - `iter_borme_items(data: dict) -> list[dict]` — items enriquecidos con `seccion` (`A`|`C`) y `apartado` (nombre o `""`).
  - `name_matches(target: str, candidate: str) -> bool` — substring bidireccional normalizado.
  - `fetch_borme_day(date_str: str) -> list[dict]`.

- [ ] **Step 1: Escribir los tests que fallan**

Añade al inicio de `tests/tools/test_borme_search.py` (mantén el runner del final, añadiendo las funciones nuevas a la lista `tests`) y **reemplaza** `test_matches_target_nif_exact_and_name_substring` por tests de `iter_borme_items` y `name_matches`:

```python
# Fixture: estructura real del sumario BORME
REAL_BORME_SUMARIO = {
    "status": {"code": "200", "text": "ok"},
    "data": {
        "sumario": {
            "diario": [
                {
                    "numero": "170",
                    "seccion": [
                        {
                            "codigo": "A",
                            "nombre": "SECCIÓN PRIMERA. Empresarios. Actos inscritos",
                            "item": [
                                {
                                    "identificador": "BORME-A-2026-170-01",
                                    "titulo": "ARABA/ÁLAVA",
                                    "url_html": "https://www.boe.es/diario_borme/txt.php?id=BORME-A-2026-170-01",
                                    "url_xml": "https://www.boe.es/diario_borme/xml.php?id=BORME-A-2026-170-01",
                                }
                            ],
                        },
                        {
                            "codigo": "C",
                            "nombre": "SECCIÓN SEGUNDA. Anuncios y avisos legales",
                            "apartado": [
                                {
                                    "codigo": "002",
                                    "nombre": "CONVOCATORIAS DE JUNTAS",
                                    "item": [
                                        {
                                            "identificador": "BORME-C-2026-4855",
                                            "titulo": "CONTRATAS Y OBRAS SAN GREGORIO, S.A.",
                                            "url_html": "https://www.boe.es/diario_borme/txt.php?id=BORME-C-2026-4855",
                                        }
                                    ],
                                }
                            ],
                        },
                    ],
                }
            ],
        }
    },
}


def test_iter_borme_items_marks_section_and_apartado():
    items = borme.iter_borme_items(REAL_BORME_SUMARIO)
    assert len(items) == 2
    assert items[0]["seccion"] == "A"
    assert items[0]["apartado"] == ""
    assert items[0]["titulo"] == "ARABA/ÁLAVA"
    assert items[1]["seccion"] == "C"
    assert items[1]["apartado"] == "CONVOCATORIAS DE JUNTAS"
    assert items[1]["titulo"] == "CONTRATAS Y OBRAS SAN GREGORIO, S.A."


def test_iter_borme_items_tolerates_empty():
    assert borme.iter_borme_items({}) == []


def test_name_matches_normalized_bidirectional():
    assert borme.name_matches("CONTRATAS Y OBRAS SAN GREGORIO", "Contratas y Obras San Gregorio, S.A.")
    assert borme.name_matches("Santander", "BANCO SANTANDER, S.A.")
    assert not borme.name_matches("BBVA", "BANCO SANTANDER, S.A.")


def test_default_lookback_days_env():
    import os as _os
    saved = _os.environ.get("BORME_LOOKBACK_DAYS")
    try:
        _os.environ.pop("BORME_LOOKBACK_DAYS", None)
        assert borme._default_lookback_days() == 7
        _os.environ["BORME_LOOKBACK_DAYS"] = "3"
        assert borme._default_lookback_days() == 3
        _os.environ["BORME_LOOKBACK_DAYS"] = "abc"
        assert borme._default_lookback_days() == 7
    finally:
        if saved is None:
            _os.environ.pop("BORME_LOOKBACK_DAYS", None)
        else:
            _os.environ["BORME_LOOKBACK_DAYS"] = saved
```

> Nota de secuencia: `matches_target` y el `search_borme` original se conservan **hasta Task 4** (Task 4 los elimina/reescribe). Hasta entonces, el `search_borme` original devolverá `[]` para items reales aplanados (porque ya no hay campos `nif`/`nombre` top-level), lo cual es un estado intermedio aceptable. Los tests obsoletos de la versión original (`test_search_builds_contract_evidence_with_raw`, `test_changed_field_missing_tolerated` y los de `matches_target`) se eliminan ya en esta Task del archivo y del runner; se reescriben en Task 4.

- [ ] **Step 2: Ejecutar el test para verificar que FALLA**

Run: `python3 -m pytest tests/tools/test_borme_search.py -q`
Expected: FAIL con `AttributeError: ... has no attribute 'iter_borme_items'`.

- [ ] **Step 3: Implementar en `tools/borme_search/main.py`**

Sustituye el bloque de constantes e imports para añadir `re`, `datetime` (ya está) y `normalize_for_match`:

```python
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common.structured_logging import get_logger
from common.http import request_with_retry, REQUESTS_AVAILABLE
from common.evidence import build_evidence, build_search_result
from common.entity_normalizer import normalize_for_match
```

Añade tras `BORME_API`:

```python
BORME_MAX_RANGE_DAYS = 90
DEFAULT_COUNT = 20


def _default_lookback_days() -> int:
    """Return BORME_LOOKBACK_DAYS (default 7), clamped to >= 1."""
    try:
        return max(1, int(os.environ.get("BORME_LOOKBACK_DAYS", "7")))
    except (TypeError, ValueError):
        return 7


def _as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def iter_borme_items(data: dict) -> list[dict]:
    """Flatten the BORME sumario into enriched item dicts.

    Section A items sit at seccion.item[] (titulo = province); section C
    items sit at seccion.apartado[].item[] (titulo = entity).
    Each item carries 'seccion' ('A'|'C') and 'apartado' (name or '').
    """
    out: list[dict] = []
    try:
        diario = data["data"]["sumario"]["diario"]
    except (KeyError, TypeError):
        return out
    for day in _as_list(diario):
        for seccion in _as_list(day.get("seccion")):
            sec_code = seccion.get("codigo", "")
            for it in _as_list(seccion.get("item")):
                if isinstance(it, dict):
                    enriched = dict(it)
                    enriched["seccion"] = sec_code
                    enriched["apartado"] = ""
                    out.append(enriched)
            for ap in _as_list(seccion.get("apartado")):
                for it in _as_list(ap.get("item")):
                    if isinstance(it, dict):
                        enriched = dict(it)
                        enriched["seccion"] = sec_code
                        enriched["apartado"] = ap.get("nombre", "")
                        out.append(enriched)
    return out


def name_matches(target: str, candidate: str) -> bool:
    """Bidirectional normalized substring match for company names."""
    tn = normalize_for_match(target)
    cn = normalize_for_match(candidate)
    return bool(tn and cn and (tn in cn or cn in tn))
```

Reemplaza `date_range` para usar el lookback configurable:

```python
def date_range(from_str: str, to_str: str) -> list[str]:
    fmt = "%Y-%m-%d"
    start = datetime.strptime(from_str, fmt) if from_str else datetime.now() - timedelta(days=_default_lookback_days())
    end = datetime.strptime(to_str, fmt) if to_str else datetime.now()
    dates = []
    current = start
    while current <= end:
        dates.append(current.strftime("%Y%m%d"))
        current += timedelta(days=1)
    return dates
```

Reemplaza `fetch_borme_day`:

```python
def fetch_borme_day(date_str: str) -> list[dict]:
    url = f"{BORME_API}/{date_str}"
    try:
        resp = request_with_retry(
            "GET", url, headers={"Accept": "application/json"}, timeout=15
        )
    except Exception as exc:
        logger.warning(
            "BORME request failed",
            extra_data={"date": date_str, "error": str(exc)},
        )
        return []
    if resp.status_code != 200:
        return []
    try:
        return iter_borme_items(resp.json())
    except Exception as exc:
        logger.warning(
            "BORME parse failed",
            extra_data={"date": date_str, "error": str(exc)},
        )
        return []
```

> Conserva `matches_target` por ahora: el `search_borme` original aún lo usa y no se reescribe hasta Task 4.
- [ ] **Step 4: Ejecutar el test para verificar que PASA**

Run: `python3 -m pytest tests/tools/test_borme_search.py -q`
Expected: los 4 tests nuevos PASS (los demás de la lista ajustada también).

- [ ] **Step 5: Commit**

```bash
git add tools/borme_search/main.py tests/tools/test_borme_search.py
git commit -m "fix(tools): flatten real BORME sumario and add configurable lookback"
```

---

### Task 3: `borme_search` — parsear el XML por provincia

**Files:**
- Modify: `tools/borme_search/main.py`
- Test: `tests/tools/test_borme_search.py`

**Interfaces:**
- Consumes: `iter_borme_items`, `name_matches` (Task 2); `xml.etree.ElementTree as ET` (nuevo import).
- Produces:
  - `parse_province_xml(xml_text: str) -> list[dict]` — `[{"name": str, "acts": [str, ...]}, ...]`.
  - `_clean_company_name(raw: str) -> str` — quita prefijo `NÚMERO - ` y punto final.
  - `fetch_province_xml(url_xml: str) -> str | None`.

- [ ] **Step 1: Escribir los tests que fallan**

Añade (y agrega a la lista `tests` del runner):

```python
PROVINCE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<documento fecha_actualizacion="20260903T071717Z">
  <metadatos>
    <identificador>BORME-A-2026-170-01</identificador>
    <titulo>ARABA/ÁLAVA</titulo>
    <seccion codigo="A">SECCIÓN PRIMERA. Empresarios. Actos inscritos</seccion>
    <fecha_publicacion>20260903</fecha_publicacion>
  </metadatos>
  <texto>
    <p class="articulo">401698 - QUALIS CONSULTORES DE TALENTO SOCIEDAD LIMITADA.</p>
    <p class="parrafo">Declaración de unipersonalidad. Socio único: TRISKELION INVESTMENTS SL.</p>
    <p class="articulo">401699 - SEÑALIZACION Y BALIZAMIENTOS JUNDIZ, SOCIEDAD LIMITADA.</p>
    <p class="parrafo">Sociedad unipersonal. Cambio de identidad del socio único.</p>
  </texto>
</documento>
"""


def test_parse_province_xml_groups_companies_and_acts():
    companies = borme.parse_province_xml(PROVINCE_XML)
    assert len(companies) == 2
    assert companies[0]["name"] == "QUALIS CONSULTORES DE TALENTO SOCIEDAD LIMITADA"
    assert companies[0]["acts"] == ["Declaración de unipersonalidad. Socio único: TRISKELION INVESTMENTS SL."]
    assert companies[1]["name"] == "SEÑALIZACION Y BALIZAMIENTOS JUNDIZ, SOCIEDAD LIMITADA"
    assert companies[1]["acts"] == ["Sociedad unipersonal. Cambio de identidad del socio único."]


def test_parse_province_xml_invalid_returns_empty():
    assert borme.parse_province_xml("<not-xml") == []
    assert borme.parse_province_xml("") == []


def test_clean_company_name_strips_number_and_dot():
    assert borme._clean_company_name("401698 - QUALIS SOCIEDAD LIMITADA.") == "QUALIS SOCIEDAD LIMITADA"
    assert borme._clean_company_name("Sin numero.") == "Sin numero"
```

- [ ] **Step 2: Ejecutar el test para verificar que FALLA**

Run: `python3 -m pytest tests/tools/test_borme_search.py -q`
Expected: FAIL con `AttributeError: ... has no attribute 'parse_province_xml'`.

- [ ] **Step 3: Implementar en `tools/borme_search/main.py`**

Añade import `import xml.etree.ElementTree as ET` y tras `name_matches`:

```python
_COMPANY_PREFIX_RE = re.compile(r'^\s*\d+\s*[-–]\s*')


def _clean_company_name(raw: str) -> str:
    text = (raw or "").strip()
    text = _COMPANY_PREFIX_RE.sub("", text)
    return text.rstrip(".").strip()


def parse_province_xml(xml_text: str) -> list[dict]:
    """Parse a BORME province XML into [{'name', 'acts'}, ...].

    Company headings are <p class="articulo"> ("NNN - NAME.") followed by
    one or more <p class="parrafo"> with the registered acts.
    """
    companies: list[dict] = []
    if not xml_text:
        return companies
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.warning(
            "BORME province XML parse failed",
            extra_data={"error": str(exc)},
        )
        return companies
    current: dict | None = None
    for p in root.iter("p"):
        cls = (p.get("class") or "").strip()
        text = "".join(p.itertext()).strip()
        if not text:
            continue
        if cls == "articulo":
            current = {"name": _clean_company_name(text), "acts": []}
            companies.append(current)
        elif cls == "parrafo" and current is not None:
            current["acts"].append(text)
    return companies


def fetch_province_xml(url_xml: str) -> str | None:
    """GET a BORME province XML. Returns text, or None on failure."""
    if not url_xml:
        return None
    try:
        resp = request_with_retry(
            "GET",
            url_xml,
            headers={"Accept": "application/xml, text/xml, */*"},
            timeout=15,
            max_retries=2,
        )
    except Exception as exc:
        logger.warning(
            "BORME province fetch failed",
            extra_data={"url": url_xml, "error": str(exc)},
        )
        return None
    if resp.status_code != 200:
        logger.warning(
            "BORME province HTTP error",
            extra_data={"url": url_xml, "status_code": resp.status_code},
        )
        return None
    return resp.text
```

- [ ] **Step 4: Ejecutar el test para verificar que PASA**

Run: `python3 -m pytest tests/tools/test_borme_search.py -q`
Expected: tests nuevos PASS.

- [ ] **Step 5: Commit**

```bash
git add tools/borme_search/main.py tests/tools/test_borme_search.py
git commit -m "feat(tools): parse BORME province XML into company acts"
```

---

### Task 4: `borme_search` — flujo de búsqueda (Sección C directa, Sección A vía XML, rechazo NIF)

**Files:**
- Modify: `tools/borme_search/main.py`
- Test: `tests/tools/test_borme_search.py` (reescritura final del runner)

**Interfaces:**
- Consumes: `iter_borme_items`, `name_matches`, `parse_province_xml`, `fetch_province_xml`, `_default_lookback_days`, `DEFAULT_COUNT`, `BORME_MAX_RANGE_DAYS`.
- Produces:
  - `_evidence_from_secondary_item(item, target, day_iso) -> list[dict] | None`
  - `_search_primary_day(items, target, count, day_iso) -> list[dict]`
  - `search_borme(target, target_type, date_from, date_to, count=None) -> tuple[list[dict] | None, str | None]` — firma ampliada con `count` opcional (compatible hacia atrás).

- [ ] **Step 1: Escribir los tests de flujo que fallan**

Mantén TODO el contenido acumulado de Tasks 2-3 en `tests/tools/test_borme_search.py` (fixtures `REAL_BORME_SUMARIO`, `PROVINCE_XML` y sus tests). Elimina los tests obsoletos que quedaron de la versión original y que referencian el modelo viejo (`test_search_builds_contract_evidence_with_raw`, `test_changed_field_missing_tolerated` y cualquier test de `matches_target`). Añade los tests de flujo:

```python
def test_search_borme_section_c_direct_match_no_xml():
    items = borme.iter_borme_items(REAL_BORME_SUMARIO)
    borme.fetch_borme_day = lambda d: items  # type: ignore[assignment]
    results, err = borme.search_borme(
        "SAN GREGORIO", "name", "2026-09-03", "2026-09-03", count=10
    )
    assert err is None
    assert len(results) == 1
    ev = results[0]
    assert ev["source"] == "borme"
    assert ev["title"] == "CONTRATAS Y OBRAS SAN GREGORIO, S.A."
    assert ev["metadata"]["seccion"] == "C"
    assert ev["metadata"]["apartado"] == "CONVOCATORIAS DE JUNTAS"
    assert ev["date"] == "2026-09-03"


def test_search_borme_section_a_uses_province_xml(monkeypatch):
    items = borme.iter_borme_items(REAL_BORME_SUMARIO)
    a_items = [it for it in items if it["seccion"] == "A"]
    borme.fetch_borme_day = lambda d: a_items  # type: ignore[assignment]
    monkeypatch.setattr(borme, "fetch_province_xml", lambda url: PROVINCE_XML)
    results, err = borme.search_borme(
        "QUALIS", "name", "2026-09-03", "2026-09-03", count=10
    )
    assert err is None
    assert len(results) == 1
    ev = results[0]
    assert ev["source"] == "borme"
    assert ev["title"] == "QUALIS CONSULTORES DE TALENTO SOCIEDAD LIMITADA"
    assert ev["metadata"]["seccion"] == "A"
    assert ev["metadata"]["provincia"] == "ARABA/ÁLAVA"
    assert ev["metadata"]["actos"] == ["Declaración de unipersonalidad. Socio único: TRISKELION INVESTMENTS SL."]
    assert ev["date"] == "2026-09-03"


def test_search_borme_rejects_nif():
    results, err = borme.search_borme("B12345678", "nif", "2026-09-03", "2026-09-03")
    assert results is None
    assert err is not None
    assert "NIF" in err


def test_search_borme_empty_success_zero():
    borme.fetch_borme_day = lambda d: []  # type: ignore[assignment]
    results, err = borme.search_borme("NADIE", "name", "2025-01-01", "2025-01-01")
    assert err is None
    assert results == []
```

> Sustituye el runner `if __name__ == "__main__":` del final por la versión pytest (para que `test_default_lookback_days_env` y cualquier test futuro con fixtures funcionen sin fricción):

```python
if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
```

- [ ] **Step 2: Ejecutar el test para verificar que FALLA**

Run: `python3 -m pytest tests/tools/test_borme_search.py -q`
Expected: FAIL con `TypeError: search_borme() got an unexpected keyword argument 'count'` o similar.

- [ ] **Step 3: Implementar el flujo en `tools/borme_search/main.py`**

Tras `fetch_province_xml` añade:

```python
def _evidence_from_secondary_item(item: dict, target: str, day_iso: str) -> list[dict] | None:
    """Section C: match on the sumario item title, no XML download."""
    title = str(item.get("titulo", ""))
    if not name_matches(target, title):
        return None
    return [build_evidence(
        source="borme",
        official=True,
        confidence=0.9,
        title=title,
        date=day_iso,
        url=str(item.get("url_html", "") or ""),
        metadata={
            "identificador": item.get("identificador", ""),
            "seccion": item.get("seccion", "C"),
            "apartado": item.get("apartado", ""),
        },
        entity=target,
        query=target,
    )]


def _search_primary_day(
    items: list[dict], target: str, count: int, day_iso: str
) -> list[dict]:
    """Section A: fetch each province XML concurrently and match company names."""
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = [ex.submit(fetch_province_xml, it.get("url_xml", "")) for it in items]
        for it, fut in zip(items, futures):
            xml_text = fut.result()
            if not xml_text:
                continue
            for comp in parse_province_xml(xml_text):
                if not name_matches(target, comp["name"]):
                    continue
                results.append(build_evidence(
                    source="borme",
                    official=True,
                    confidence=0.9,
                    title=comp["name"],
                    date=day_iso,
                    url=str(it.get("url_html", "") or ""),
                    metadata={
                        "identificador": it.get("identificador", ""),
                        "provincia": it.get("titulo", ""),
                        "seccion": it.get("seccion", "A"),
                        "actos": comp["acts"],
                    },
                    entity=target,
                    query=target,
                ))
                if len(results) >= count:
                    return results
    return results
```

Reemplaza `search_borme` (y elimina la función obsoleta `matches_target`, que ya no usa nadie):

```python
def search_borme(
    target: str,
    target_type: str,
    date_from: str,
    date_to: str,
    count: int | None = None,
) -> tuple[list[dict] | None, str | None]:
    if not REQUESTS_AVAILABLE:
        return None, "requests library not available"
    if not target:
        return None, "el parámetro 'target' es obligatorio"
    if target_type in ("nif", "cif"):
        return None, (
            "BORME no publica NIF/CIF (enmascarados por protección de datos); "
            "busque por nombre de empresa."
        )
    if count is None or count <= 0:
        count = DEFAULT_COUNT
    dates = date_range(date_from, date_to)
    if len(dates) > BORME_MAX_RANGE_DAYS:
        return None, f"date range too large (max {BORME_MAX_RANGE_DAYS} days)"
    results: list[dict] = []
    for d in dates:
        day_iso = datetime.strptime(d, "%Y%m%d").strftime("%Y-%m-%d")
        items = fetch_borme_day(d)
        primary = [it for it in items if it.get("seccion") == "A"]
        secondary = [it for it in items if it.get("seccion") == "C"]
        for it in secondary:
            evs = _evidence_from_secondary_item(it, target, day_iso)
            if evs:
                results.extend(evs)
                if len(results) >= count:
                    return results[:count], None
        if len(results) >= count:
            return results[:count], None
        for ev in _search_primary_day(primary, target, count - len(results), day_iso):
            results.append(ev)
            if len(results) >= count:
                return results[:count], None
    return results[:count], None
```

Actualiza `main()` para leer `count` y validar `target_type` con default `name` (reemplaza el bloque desde `target_type = args.get(...)` hasta la llamada a `search_borme`):

```python
        target_type = args.get("target_type", "name")
        if target_type not in ("nif", "cif", "name"):
            target_type = "name"
        date_from = args.get("date_from", "")
        date_to = args.get("date_to", "")
        count = args.get("count", DEFAULT_COUNT)
        try:
            count = int(count)
        except (TypeError, ValueError):
            count = DEFAULT_COUNT
        if count <= 0:
            count = DEFAULT_COUNT

        results, error = search_borme(
            target, target_type, date_from, date_to, count
        )
```

- [ ] **Step 4: Ejecutar el test para verificar que PASA**

Run: `python3 -m pytest tests/tools/test_borme_search.py -q`
Expected: PASS (11 tests).

- [ ] **Step 5: Commit**

```bash
git add tools/borme_search/main.py tests/tools/test_borme_search.py
git commit -m "feat(tools): search BORME by company name across section A and C"
```

---

### Task 5: `tool.yaml` — descripciones, timeout y esquema

**Files:**
- Modify: `tools/boe_search/tool.yaml`
- Modify: `tools/borme_search/tool.yaml`

**Interfaces:**
- Consumes: nada de código (solo manifiestos).
- Produces: manifiestos actualizados que el discovery Go leerá.

- [ ] **Step 1: Actualizar `tools/boe_search/tool.yaml`**

Reemplaza `description` (documenta la limitación real) por:

```yaml
description: "Busca disposiciones y actos en BOE por NIF/nombre en títulos del sumario oficial (datos abiertos). Limitación: la API no expone la Sección V (Anuncios) ni Administración de Justicia; solo disposiciones, autoridades/personal y otras disposiciones."
```

- [ ] **Step 2: Actualizar `tools/borme_search/tool.yaml`**

Reemplaza `description`, `timeout` y el bloque `input_schema` por:

```yaml
description: "Busca actos mercantiles en BORME por NOMBRE de empresa (el BORME abierto no publica NIF/CIF). Descarga sumarios por rango y extrae actos desde los XML por provincia (Sección Primera) y anuncios (Sección Segunda)."
command: python3
args:
  - main.py
timeout: 300s
input_schema:
  type: object
  properties:
    target:
      type: string
      description: "Nombre de empresa a buscar"
    target_type:
      type: string
      enum: [nif, cif, name]
      default: name
      description: "Solo 'name' está soportado; 'nif'/'cif' devuelven SEARCH_FAILED (BORME no publica NIF)."
    date_from:
      type: string
      description: "Inicio del rango (YYYY-MM-DD). Default: BORME_LOOKBACK_DAYS días atrás."
    date_to:
      type: string
      description: "Fin del rango (YYYY-MM-DD). Default: hoy."
    count:
      type: integer
      default: 20
      description: "Máximo de resultados a devolver."
  required:
    - target
```

- [ ] **Step 3: Verificar que el discovery valida los manifiestos**

Run: `go test ./internal/config/... -run TestDiscover -v 2>&1 | tail -20`
Expected: PASS (el discovery parsea ambos `tool.yaml` sin error).

- [ ] **Step 4: Commit**

```bash
git add tools/boe_search/tool.yaml tools/borme_search/tool.yaml
git commit -m "docs(tools): reflect BORME name-only search and BOE sumario limits"
```

---

### Task 6: Verificación completa

**Files:** ninguno (solo ejecución).

- [ ] **Step 1: Suite Python completa**

Run: `python3 -m pytest tests/ -q`
Expected: PASS (62 tests previos ajustados por los nuevos, sin regresiones).

- [ ] **Step 2: Suite Go (no debe cambiar, semáforo de regresión)**

Run: `go test ./... && go vet ./... && go build ./cmd/server/`
Expected: todos `ok`.

- [ ] **Step 3: Smoke test en vivo `boe_search`**

Run:
```sh
echo '{"request_id":"t","arguments":{"target":"Banco Santander","target_type":"name","date_from":"2026-08-01","date_to":"2026-08-31"}}' | python3 tools/boe_search/main.py
```
Expected: `count >= 0` real; si hay resultados, texto con disposiciones que mencionan el target. Nunca más `count: 0` por bug de parsing (un `0` legítimo va acompañado de "No se encontraron resultados.").

- [ ] **Step 4: Smoke test en vivo `borme_search`**

Run:
```sh
echo '{"request_id":"t","arguments":{"target":"Santander","target_type":"name","date_from":"2026-08-01","date_to":"2026-08-31"}}' | python3 tools/borme_search/main.py
```
Expected: `success: true`; puede tardar (descarga XML de provincias). Si no hay matches, texto "No se encontraron resultados.".

- [ ] **Step 5: Registro en memoria (solo si hay hallazgos nuevos)**

Si el smoke test revela un matiz de la API no documentado (p. ej. formato de fecha distinto, otra sección), actualiza el spec `2026-09-07-fase5-1-reparar-fuentes-design.md` y haz commit separado `docs(specs): ...`.
