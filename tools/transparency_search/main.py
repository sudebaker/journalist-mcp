#!/usr/bin/env python3
"""Portal de Transparencia search tool.

Scrapea el buscador de publicidad activa del Portal de Transparencia
(https://www.transparencia.gob.es) usando html.parser stdlib.

Estrategia:
  - GET a /servicios-buscador/buscar.htm con ?q=target&pag=N
  - Parsear resultados con HTMLParser. El portal (rediseñado, sin www) usa:
      contenedor .div_busq, título .title_justif (con <a href>), categoría
      .letra_grisacea.h5Size y descripción .letra_grisacea.min_parr.h6Size.
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

logger = get_logger(__name__, "transparency_search")

BASE_URL = "https://transparencia.gob.es"
BUSCADOR_URL = f"{BASE_URL}/servicios-buscador/buscar.htm"
SOURCE = "TRANSPARENCIA"
MAX_PAGES = 10


class TransparenciaParser(HTMLParser):
    """Extrae filas de resultados del HTML del buscador del Portal.

    Estructura observada en runtime (rediseño 2025 del portal):
      <div class="div_busq">
        <p class="title_justif h4Size"><a href="...">Título</a></p>
        <p class="letra_grisacea h5Size">Categoría</p>
        <p class="letra_grisacea min_parr h6Size">Descripción/detalle</p>
      </div>
    """

    _RESULT_CLASS = "div_busq"

    def __init__(self):
        super().__init__()
        self.results: list[dict[str, str]] = []
        self._in_result = False
        self._current: dict[str, str] = {}
        self._depth = 0
        self._capture: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        attrs_d = dict(attrs)
        cls = (attrs_d.get("class") or "").lower()
        classes = cls.split()
        if self._RESULT_CLASS in classes:
            self._in_result = True
            self._depth = 1
            self._current = {}
            return
        if not self._in_result:
            return
        self._depth += 1
        if tag == "a" and "href" in attrs_d:
            href = attrs_d["href"]
            if href and not href.startswith("#"):
                self._current["url"] = urljoin(BASE_URL, href)
            self._capture = "title"
        elif "title_justif" in classes:
            self._capture = "title"
        elif "letra_grisacea" in classes and "min_parr" in classes:
            self._capture = "description"
        elif "letra_grisacea" in classes:
            self._capture = "categoria"

    def handle_data(self, data: str):
        if self._capture and self._in_result:
            text = " ".join(data.split())
            if text:
                prev = self._current.get(self._capture, "")
                self._current[self._capture] = (prev + " " + text).strip()

    def handle_endtag(self, tag: str):
        if not self._in_result:
            return
        if self._capture and tag in ("a", "p", "h3", "h4"):
            self._capture = None
        self._depth -= 1
        if self._depth <= 0:
            if any(self._current.values()):
                self.results.append(dict(self._current))
            self._current = {}
            self._in_result = False


def _search_publicidad_activa(
    target: str, date_from: str, date_to: str, count: int,
) -> tuple[list[dict], str | None]:
    results: list[dict] = []
    page = 1
    while len(results) < count and page <= MAX_PAGES:
        params: dict[str, Any] = {"q": target, "pag": page}
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
            # El buscador del portal ya filtra server-side por ?q=target.
            # No aplicamos matching local estricto: los títulos relevantes
            # pueden no contener la frase exacta (ej. "Ministerio de Hacienda").
            ev = build_evidence(
                source=SOURCE,
                official=True,
                confidence=0.9,
                title=title,
                date=r.get("date", ""),
                url=r.get("url", ""),
                metadata={
                    "categoria": r.get("categoria", ""),
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
                if m.get("categoria"):
                    lines.append(f"   - Categoría: {m['categoria']}")
                if m.get("descripcion"):
                    lines.append(f"   - Detalle: {m['descripcion']}")
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
