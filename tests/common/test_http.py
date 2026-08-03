#!/usr/bin/env python3
"""Smoke tests for tools/common/http.py."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))

from common.http import (
    REQUESTS_AVAILABLE,
    _URLLIB3_RETRY_AVAILABLE,
    get_session,
    request_with_retry,
)

KNOW_GOOD_URL = "https://www.google.com"


@pytest.mark.skipif(not REQUESTS_AVAILABLE, reason="requests not installed")
def test_get_session_returns_configured_session():
    session = get_session()
    assert session.headers["User-Agent"].startswith("journalist-mcp/")
    assert "Accept" in session.headers


@pytest.mark.skipif(not REQUESTS_AVAILABLE, reason="requests not installed")
def test_request_with_retry_get_200():
    response = request_with_retry("GET", KNOW_GOOD_URL, timeout=15, max_retries=2)
    assert response.status_code == 200


@pytest.mark.skipif(not REQUESTS_AVAILABLE, reason="requests not installed")
def test_user_agent_constant():
    from common.http import _USER_AGENT

    assert "journalist-mcp" in _USER_AGENT
    assert "github.com/sudebaker" in _USER_AGENT


def test_urllib3_retry_availability():
    assert isinstance(_URLLIB3_RETRY_AVAILABLE, bool)


if __name__ == "__main__":
    if not REQUESTS_AVAILABLE:
        print("requests not available — skipping", file=sys.stderr)
        sys.exit(0)

    print(f"urllib3 Retry available: {_URLLIB3_RETRY_AVAILABLE}", file=sys.stderr)

    session = get_session()
    print(f"Session UA: {session.headers['User-Agent']}", file=sys.stderr)

    response = request_with_retry("GET", KNOW_GOOD_URL, timeout=15, max_retries=2)
    print(f"HTTP {response.status_code}", file=sys.stderr)
    assert response.status_code == 200, f"Expected 200, got {response.status_code}"
    print("Smoke test PASSED", file=sys.stderr)
