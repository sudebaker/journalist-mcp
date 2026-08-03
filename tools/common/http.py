#!/usr/bin/env python3
"""Shared HTTP session helper with retry/backoff for OSINT tools.

Provides a configured requests.Session and a request_with_retry wrapper that
handles 429/503 Retry-After headers, exponential backoff, and graceful
fallback when urllib3 Retry is unavailable.

Dependencies: requests + stdlib only.
stdout is reserved for the MCP protocol JSON — all logging goes to stderr.
"""

import os
import sys
import time
from typing import Any, Dict, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from common.structured_logging import get_logger

logger = get_logger(__name__, "http")

try:
    import requests
    from requests.adapters import HTTPAdapter
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False

try:
    from urllib3.util.retry import Retry
    _URLLIB3_RETRY_AVAILABLE = True
except (ImportError, AttributeError):
    _URLLIB3_RETRY_AVAILABLE = False

_USER_AGENT = "journalist-mcp/1.0 (+https://github.com/sudebaker/journalist-mcp)"

_DEFAULT_STATUS_FORCELIST = [429, 500, 502, 503, 504]
_BACKOFF_FACTOR = 0.5
_POOL_CONNECTIONS = 10
_POOL_MAXSIZE = 10

__all__ = ["get_session", "request_with_retry", "REQUESTS_AVAILABLE",
           "_URLLIB3_RETRY_AVAILABLE"]


def get_session() -> "requests.Session":
    """Create a configured requests.Session.

    Sets User-Agent and Accept headers, and configures connection pooling.
    """
    session = requests.Session()
    session.headers.update({
        "User-Agent": _USER_AGENT,
        "Accept": "application/json, text/html, text/plain, */*",
    })
    adapter = HTTPAdapter(
        pool_connections=_POOL_CONNECTIONS,
        pool_maxsize=_POOL_MAXSIZE,
        max_retries=0,
    )
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def _build_retry_adapter(max_retries: int) -> HTTPAdapter:
    """Build an HTTPAdapter with urllib3 Retry for 429/503 + Retry-After."""
    retry = Retry(
        total=max_retries,
        backoff_factor=_BACKOFF_FACTOR,
        status_forcelist=_DEFAULT_STATUS_FORCELIST,
        respect_retry_after_header=True,
        raise_on_status=False,
        allowed_methods=frozenset([
            "GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS"
        ]),
    )
    return HTTPAdapter(
        pool_connections=_POOL_CONNECTIONS,
        pool_maxsize=_POOL_MAXSIZE,
        max_retries=retry,
    )


def _simple_retry_request(
    session: "requests.Session",
    method: str,
    url: str,
    params: Optional[Dict[str, Any]],
    json_body: Optional[Any],
    headers: Dict[str, Any],
    data: Optional[Any],
    timeout: int,
    max_retries: int,
) -> "requests.Response":
    """Simple retry loop fallback when urllib3 Retry is unavailable.

    Mimics the behavior of urllib3 Retry: retries on 429/500/502/503/504,
    honors Retry-After headers, and uses exponential backoff.
    Returns the final response without raising on transient HTTP errors.
    """
    last_response: Optional["requests.Response"] = None
    last_error: Optional[Exception] = None

    for attempt in range(max_retries + 1):
        try:
            response = session.request(
                method=method,
                url=url,
                params=params,
                json=json_body,
                headers=headers,
                data=data,
                timeout=timeout,
            )
            last_response = response

            if response.status_code not in _DEFAULT_STATUS_FORCELIST:
                return response

            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    wait_time = float(retry_after)
                except (ValueError, TypeError):
                    wait_time = _BACKOFF_FACTOR * (2 ** attempt)
            else:
                wait_time = _BACKOFF_FACTOR * (2 ** attempt)

            if attempt < max_retries:
                logger.warning(
                    f"HTTP transient status {response.status_code} on attempt {attempt + 1}/{max_retries + 1}",
                    extra_data={
                        "http_method": method,
                        "url": url,
                        "status_code": response.status_code,
                        "attempt": attempt + 1,
                        "max_retries": max_retries,
                        "wait_seconds": round(wait_time, 4),
                    },
                )
                time.sleep(wait_time)

        except requests.exceptions.RequestException as exc:
            last_error = exc
            if attempt < max_retries:
                wait_time = _BACKOFF_FACTOR * (2 ** attempt)
                logger.warning(
                    f"Request error on attempt {attempt + 1}/{max_retries + 1}",
                    extra_data={
                        "http_method": method,
                        "url": url,
                        "attempt": attempt + 1,
                        "max_retries": max_retries,
                        "wait_seconds": round(wait_time, 4),
                        "error": str(exc),
                    },
                )
                time.sleep(wait_time)
            continue

    if last_response is not None:
        return last_response

    if last_error is not None:
        raise last_error

    return None  # type: ignore[return-value]


def request_with_retry(
    method: str,
    url: str,
    params: Optional[Dict[str, Any]] = None,
    json: Optional[Any] = None,
    headers: Optional[Dict[str, Any]] = None,
    data: Optional[Any] = None,
    timeout: int = 30,
    max_retries: int = 3,
) -> "requests.Response":
    """Perform an HTTP request with retry/backoff and 429/503 Retry-After handling.

    Uses urllib3 Retry when available (with 429/503 Retry-After support).
    Falls back to a simple retry loop otherwise.

    Args:
        method: HTTP method (GET, POST, etc.)
        url: Target URL
        params: Query parameters
        json: JSON body
        headers: Additional headers (merged with defaults)
        data: Raw request body
        timeout: Request timeout in seconds
        max_retries: Maximum number of retry attempts

    Returns:
        requests.Response. Does not raise on transient HTTP errors (429/5xx);
        the caller receives the final response with the status code.
    """
    if not REQUESTS_AVAILABLE:
        raise RuntimeError("requests library is not available")

    session = get_session()

    request_headers: Dict[str, Any] = dict(headers) if headers else {}

    start_time = time.monotonic()
    logger.info(
        f"HTTP request: {method} {url}",
        extra_data={
            "http_method": method,
            "url": url,
            "timeout": timeout,
            "max_retries": max_retries,
        },
    )

    if _URLLIB3_RETRY_AVAILABLE:
        adapter = _build_retry_adapter(max_retries)
        session.mount("http://", adapter)
        session.mount("https://", adapter)

        response = session.request(
            method=method,
            url=url,
            params=params,
            json=json,
            headers=request_headers,
            data=data,
            timeout=timeout,
        )
    else:
        response = _simple_retry_request(
            session, method, url, params, json, request_headers,
            data, timeout, max_retries,
        )

    duration = time.monotonic() - start_time
    logger.info(
        f"HTTP response: {method} {url} -> {response.status_code}",
        extra_data={
            "http_method": method,
            "url": url,
            "status_code": response.status_code,
            "duration_seconds": round(duration, 4),
            "retry_backend": (
                "urllib3_retry" if _URLLIB3_RETRY_AVAILABLE else "simple_loop"
            ),
        },
    )

    return response
