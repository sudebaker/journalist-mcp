#!/usr/bin/env python3
"""Smoke test for tools/doue_search.

Verifica la construcción de la expert query y del sobre SOAP sin depender de
credenciales EUR-Lex. Si las variables de entorno EUR_LEX_USERNAME y
EUR_LEX_PASSWORD están definidas, realiza una única llamada al WebService SOAP
real y reporta el estado + un aviso de ejemplo.

Ejecución:  python3 tests/tools/test_doue_search.py
"""

import os
import sys

HERE = os.path.dirname(__file__)
TOOLS = os.path.normpath(os.path.join(HERE, "..", "..", "tools"))
sys.path.insert(0, TOOLS)

from doue_search.main import (  # noqa: E402
    build_expert_query,
    build_soap_envelope,
    parse_response,
    search_doue,
    format_date,
    SOAPACTION,
    WSDL_URL,
)


def test_date_format():
    assert format_date("2025-01-01") == "01/01/2025", format_date("2025-01-01")
    assert format_date("2026-01-01") == "01/01/2026", format_date("2026-01-01")
    print("PASS test_date_format")


def test_expert_query_name():
    q = build_expert_query("Banco Santander", "name", "2025-01-01", "2026-01-01")
    assert 'DN="Banco Santander"' in q, q
    assert "DD >= 01/01/2025" in q, q
    assert "DD <= 01/01/2026" in q, q
    print("PASS test_expert_query_name")


def test_expert_query_nif():
    q = build_expert_query("B12345678", "nif", "2025-01-01", "2026-01-01")
    assert 'DN=B12345678' in q, q
    assert "DD >= 01/01/2025" in q, q
    print("PASS test_expert_query_nif")


def test_expert_query_escape():
    q = build_expert_query('Foo"Bar', "name", "", "")
    assert '\\"Bar' in q, q
    print("PASS test_expert_query_escape")


def test_envelope_structure():
    env = build_soap_envelope(
        'DN="Banco Santander"', 1, 3, "es",
        "user", "pass")
    assert "http://www.w3.org/2003/05/soap-envelope" in env
    assert "<sear:searchRequest" in env
    assert "<sear:expertQuery>" in env
    assert "<sear:page>1</sear:page>" in env
    assert "<sear:pageSize>3</sear:pageSize>" in env
    assert "<sear:searchLanguage>es</sear:searchLanguage>" in env
    assert "wsse:Security" in env
    assert "wsse:UsernameToken" in env
    assert "<wsse:Username>user</wsse:Username>" in env
    assert "<wsse:Password" in env
    assert ">pass</wsse:Password>" in env
    print("PASS test_envelope_structure")


def test_envelope_xml_escape():
    # El carácter < debe escaparse para que el sobre sea XML válido.
    env = build_soap_envelope("a < b", 1, 3, "es", "u", "p")
    from xml.etree import ElementTree as ET
    ET.fromstring(env)  # no debe lanzar
    assert "< b" not in env
    print("PASS test_envelope_xml_escape")


def test_envelope_no_credentials():
    env = build_soap_envelope("x", 1, 1, "es", "", "")
    assert "wsse:Security" not in env or "UsernameToken" not in env
    from xml.etree import ElementTree as ET
    ET.fromstring(env)
    print("PASS test_envelope_no_credentials")


def test_parse_fault():
    fault_xml = (
        '<?xml version="1.0"?>'
        '<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope">'
        '<soap:Body><soap:Fault>'
        '<soap:Code><soap:Value>soap:Sender</soap:Value></soap:Code>'
        '<soap:Reason><soap:Text>wsse:InvalidSecurity</soap:Text></soap:Reason>'
        '</soap:Fault></soap:Body></soap:Envelope>')
    parsed = parse_response(fault_xml, "es")
    assert parsed and parsed[0].get("_soap_fault") is True
    assert "InvalidSecurity" in parsed[0].get("fault", "")
    print("PASS test_parse_fault")


def _live_call():
    username = os.environ.get("EUR_LEX_USERNAME", "")
    password = os.environ.get("EUR_LEX_PASSWORD", "")
    results, error = search_doue(
        "Banco Santander", "name", "2025-01-01", "2026-01-01", 3, "es")
    print(f"\n[LIVE] search_doue results count: "
          f"{len(results) if results else 0}")
    print(f"[LIVE] error: {error}")
    if results:
        print(f"[LIVE] sample notice: {results[0]}")
    return results, error


def main():
    tests = [
        test_date_format,
        test_expert_query_name,
        test_expert_query_nif,
        test_expert_query_escape,
        test_envelope_structure,
        test_envelope_xml_escape,
        test_envelope_no_credentials,
        test_parse_fault,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"FAIL {t.__name__}: {e}", file=sys.stderr)
            failed += 1

    # Report the SOAP endpoint + action that real requests hit.
    print(f"\n[CONFIG] WSDL_URL={WSDL_URL} SOAPACTION={SOAPACTION}")

    # Live call (will return InvalidSecurity unless creds are valid).
    live_results, live_error = _live_call()
    if live_error and "Credenciales" in live_error:
        print("[LIVE] skipped: EUR-LEX_USERNAME/EUR_LEX_PASSWORD not set")

    print(f"\n{passed} passed, {failed} failed")
    # Smoke exit code: fail only if the offline unit tests fail.
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
