#!/usr/bin/env python3
"""
Crawl4AI Render Tool for MCP Orchestrator.

Renders a web page using Craw4AI's HTTP API (POST /crawl) and returns either
clean Markdown or raw HTML. Crawl4AI handles JS rendering, basic anti-bot
evasion and content extraction on the server side, so this tool is just a
thin authenticated client around it.

Follows the same SubprocessRequest/SubprocessResponse JSON-over-stdio
contract as the rest of tools/* (see tools/pscp/main.py for reference).
"""
import json
import os
import sys
from typing import Any, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common.structured_logging import get_logger

logger = get_logger(__name__, "crawl4ai_render")

try:
    import requests as _requests
    REQUESTS_AVAILABLE = True
    requests = _requests  # alias for type-checker friendliness
except ImportError:
    _requests = None  # type: ignore[assignment]
    REQUESTS_AVAILABLE = False

# Crawl4AI container in the journalist-mcp stack listens on 11235 by default
# (see deployments/docker-compose.yml). CRAWL4AI_URL lets operators point at
# a remote deployment for local dev.
CRAWL4AI_URL = os.environ.get("CRAWL4AI_URL", "http://crawl4ai:11235").strip().rstrip("/")
CRAWL4AI_TOKEN = os.environ.get("CRAWL4AI_TOKEN", "").strip()

DEFAULT_OUTPUT_FORMAT = "markdown"  # "markdown" | "html"
DEFAULT_TIMEOUT_S = 30
MAX_URL_LENGTH = 8192

VALID_OUTPUT_FORMATS = ("markdown", "html")


def read_request() -> dict[str, Any]:
    return json.loads(sys.stdin.read())


def write_response(data: dict[str, Any]) -> None:
    print(json.dumps(data, default=str), flush=True)


def _build_payload(
    url: str,
    output_format: str,
    css_selector: Optional[str],
    wait_for: Optional[str],
    timeout_s: int,
) -> dict[str, Any]:
    """Build the JSON body sent to Crawl4AI's /crawl endpoint.

    Crawl4AI accepts a flexible schema; the most common fields are mapped
    here. Extra fields like `magic` and `extraction_config` can be added
    later without breaking the tool.
    """
    payload: dict[str, Any] = {
        "url": url,
        # Crawl4AI returns `result.markdown` / `result.html` regardless, but
        # `result.formats` lets us tell the server what to extract eagerly.
        "result_formats": [output_format],
        "timeout": timeout_s,
    }
    if css_selector:
        # Crawl4AI uses `css_selector` (snake_case) to scope extraction.
        payload["css_selector"] = css_selector
    if wait_for:
        # `wait_for` is a CSS selector that must appear before the crawl
        # finishes — useful for SPAs that render content asynchronously.
        payload["wait_for"] = wait_for
    return payload


def _build_headers() -> dict[str, str]:
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if CRAWL4AI_TOKEN:
        # Crawl4AI v0.4+ expects a Bearer token. The token is also accepted
        # via the `X-Crawl4AI-Token` header for self-hosted deployments
        # that don't follow the Bearer convention.
        headers["Authorization"] = f"Bearer {CRAWL4AI_TOKEN}"
        headers["X-Crawl4AI-Token"] = CRAWL4AI_TOKEN
    return headers


def render_page(
    url: str,
    output_format: str = DEFAULT_OUTPUT_FORMAT,
    css_selector: Optional[str] = None,
    wait_for: Optional[str] = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> tuple[Optional[str], Optional[str], Optional[dict[str, Any]]]:
    """Call Crawl4AI and return (content, error, metadata).

    `content` is the chosen output format (markdown or html) on success.
    `metadata` is a small dict with status_code, response size and any other
    useful info — surfaced to the caller in the response payload.
    """
    if not REQUESTS_AVAILABLE:
        return None, "requests library not available", None
    if not url.startswith(("http://", "https://")):
        return None, "URL must start with http:// or https://", None
    if output_format not in VALID_OUTPUT_FORMATS:
        return None, (
            f"output_format must be one of {list(VALID_OUTPUT_FORMATS)}, "
            f"got {output_format!r}"
        ), None

    payload = _build_payload(url, output_format, css_selector, wait_for, timeout_s)
    headers = _build_headers()

    try:
        response = requests.post(
            f"{CRAWL4AI_URL}/crawl",
            json=payload,
            headers=headers,
            timeout=timeout_s + 5,
        )
    except requests.exceptions.Timeout:
        return None, f"Crawl4AI timed out after {timeout_s}s", None
    except requests.exceptions.ConnectionError as e:
        return None, f"Cannot reach Crawl4AI at {CRAWL4AI_URL}: {e}", None
    except Exception as e:  # pragma: no cover - defensive
        return None, f"Crawl4AI request failed: {e}", None

    if response.status_code in (401, 403):
        return None, (
            f"Crawl4AI rejected the request (HTTP {response.status_code}) — "
            "check CRAWL4AI_TOKEN"
        ), None

    if response.status_code != 200:
        # Crawl4AI returns JSON errors with a `detail` field on 4xx; surface it
        # verbatim when present.
        try:
            detail = response.json().get("detail")
        except Exception:
            detail = None
        msg = (
            detail
            or f"Crawl4AI returned HTTP {response.status_code}"
        )
        return None, msg, None

    try:
        body = response.json()
    except ValueError as e:
        return None, f"Crawl4AI returned non-JSON response: {e}", None

    if not body.get("success", False):
        # Crawl4AI signals per-crawl failures inside a 200 envelope when
        # `success=false` is set on the body.
        err_msg = (
            body.get("error")
            or body.get("detail")
            or "Crawl4AI reported success=false without an error message"
        )
        return None, err_msg, None

    content = _extract_content(body, output_format)
    if content is None:
        return None, (
            f"Crawl4AI response did not include expected field {output_format!r} "
            "in `result`"
        ), None

    meta: dict[str, Any] = {
        "tool": "crawl4ai_render",
        "crawl4ai_status_code": body.get("status_code"),
        "crawl4ai_duration_ms": _safe_get(body, "result", "metadata", "duration_ms"),
        "crawl4ai_response_bytes": len(response.content),
        "output_format": output_format,
    }
    return content, None, meta


def _extract_content(body: dict[str, Any], output_format: str) -> Optional[str]:
    """Pull the requested field out of a Crawl4AI response envelope.

    Crawl4AI returns `{ success, result: { markdown, html, ... } }` on success.
    Older builds returned the body itself, so we fall back to top-level keys
    for compatibility.
    """
    result = body.get("result") or body
    value = result.get(output_format)
    if value is None:
        return None
    return str(value)


def _safe_get(d: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def _parse_args(args: dict[str, Any]) -> tuple[
    str, str, Optional[str], Optional[str], int
]:
    """Validate and coerce the caller's arguments."""
    url = str(args.get("url", "")).strip()
    output_format = str(
        args.get("output_format", DEFAULT_OUTPUT_FORMAT)
    ).strip() or DEFAULT_OUTPUT_FORMAT
    css_selector_raw = args.get("selector") or args.get("css_selector")
    css_selector = str(css_selector_raw).strip() if css_selector_raw else None
    wait_for_raw = args.get("wait_for")
    wait_for = str(wait_for_raw).strip() if wait_for_raw else None
    try:
        timeout_s = int(args.get("timeout_s", DEFAULT_TIMEOUT_S))
    except (TypeError, ValueError):
        timeout_s = DEFAULT_TIMEOUT_S
    timeout_s = max(1, min(timeout_s, 300))  # 1s..5min hard cap
    return url, output_format, css_selector, wait_for, timeout_s


def main() -> None:
    request: dict = {}
    try:
        request = read_request()
        request_id = request.get("request_id", "")
        args = request.get("arguments", {}) or {}

        url, output_format, css_selector, wait_for, timeout_s = _parse_args(args)

        if not url:
            write_response({
                "success": False,
                "request_id": request_id,
                "error": {
                    "code": "MISSING_URL",
                    "message": "url parameter is required",
                },
            })
            return

        if not url.startswith(("http://", "https://")):
            write_response({
                "success": False,
                "request_id": request_id,
                "error": {
                    "code": "INVALID_URL",
                    "message": "URL must start with http:// or https://",
                },
            })
            return

        content, error, metadata = render_page(
            url=url,
            output_format=output_format,
            css_selector=css_selector,
            wait_for=wait_for,
            timeout_s=timeout_s,
        )

        if error:
            write_response({
                "success": False,
                "request_id": request_id,
                "error": {"code": "RENDER_FAILED", "message": error},
            })
            return

        write_response({
            "success": True,
            "request_id": request_id,
            "content": [{"type": "text", "text": content}],
            "metadata": metadata or {"tool": "crawl4ai_render"},
        })

    except json.JSONDecodeError:
        write_response({
            "success": False,
            "request_id": "",
            "error": {"code": "INVALID_JSON", "message": "Failed to parse JSON request"},
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
