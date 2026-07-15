#!/usr/bin/env python3
"""
Browserless Render Tool for MCP Orchestrator.
Renders web pages using a headless Chrome service (browserless/chrome).
"""

import json
import os
import sys
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from common.structured_logging import get_logger

logger = get_logger(__name__, "browserless_render")

try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False

BROWSERLESS_URL = os.environ.get("BROWSERLESS_URL", "http://browserless:3000").strip().rstrip("/")
DEFAULT_TIMEOUT_MS = 30000
DEFAULT_WAIT_UNTIL = "networkidle0"
MAX_URL_LENGTH = 8192


def read_request() -> dict[str, Any]:
    return json.loads(sys.stdin.read())


def write_response(data: dict[str, Any]) -> None:
    print(json.dumps(data, default=str), flush=True)


def render_page(
    url: str,
    selector: str | None = None,
    wait_until: str = DEFAULT_WAIT_UNTIL,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> tuple[str | None, str | None]:
    """Render a web page via browserless/chrome headless service."""
    if not REQUESTS_AVAILABLE:
        return None, "requests library not available"

    if not url.startswith(("http://", "https://")):
        return None, "URL must start with http:// or https://"

    payload: dict[str, Any] = {
        "url": url,
        "options": {
            "waitUntil": wait_until,
            "timeout": timeout_ms,
        },
    }
    if selector:
        payload["options"]["selector"] = selector

    try:
        response = requests.post(
            f"{BROWSERLESS_URL}/content",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=timeout_ms // 1000 + 5,
        )
        if response.status_code != 200:
            return None, f"Browserless returned HTTP {response.status_code}"

        return response.text, None

    except requests.exceptions.Timeout:
        return None, f"Browserless timed out after {timeout_ms}ms"
    except requests.exceptions.ConnectionError as e:
        return None, f"Cannot reach browserless at {BROWSERLESS_URL}: {str(e)}"
    except Exception as e:
        return None, f"Render failed: {str(e)}"


def main() -> None:
    request: dict = {}
    try:
        request = read_request()
        request_id = request.get("request_id", "")
        args = request.get("arguments", {})

        url = str(args.get("url", "")).strip()
        if not url:
            write_response({
                "success": False,
                "request_id": request_id,
                "error": {"code": "MISSING_URL", "message": "url parameter is required"},
            })
            return

        if not url.startswith(("http://", "https://")):
            write_response({
                "success": False,
                "request_id": request_id,
                "error": {"code": "INVALID_URL", "message": "URL must start with http:// or https://"},
            })
            return

        selector = args.get("selector")
        wait_until = str(args.get("wait_until", DEFAULT_WAIT_UNTIL))
        timeout_ms = int(args.get("timeout_ms", DEFAULT_TIMEOUT_MS))

        html, error = render_page(url, selector=selector, wait_until=wait_until, timeout_ms=timeout_ms)

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
            "content": [{"type": "text", "text": html}],
            "metadata": {"tool": "browserless_render"},
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
