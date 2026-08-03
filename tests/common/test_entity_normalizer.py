#!/usr/bin/env python3
"""Tests for tools/common/entity_normalizer.py."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))

from common.entity_normalizer import normalize_for_match


class TestEntityNormalizer(unittest.TestCase):
    """Unit tests for normalize_for_match."""

    def test_accents_stripped(self):
        """Accented characters should be stripped to plain ASCII."""
        result = normalize_for_match("Bancó Santándér")
        self.assertEqual(result, "banco santander")

    def test_legal_forms_stripped(self):
        """Legal form suffixes should be stripped, not matched inside names."""
        self.assertEqual(normalize_for_match("Bancó Santándér, S.A."), "banco santander")
        self.assertEqual(normalize_for_match("Café S.L."), "cafe")
        self.assertEqual(normalize_for_match("Distribuciones S.L.U."), "distribuciones")
        self.assertEqual(
            normalize_for_match("Sociedad Anónima Test"),
            "test",
        )

    def test_lowercase_and_collapse_spaces(self):
        """Result should be lowercased and whitespace collapsed."""
        result = normalize_for_match("  Banco    Santander  ")
        self.assertEqual(result, "banco santander")

    def test_none_input(self):
        """None input should return empty string."""
        self.assertEqual(normalize_for_match(None), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
