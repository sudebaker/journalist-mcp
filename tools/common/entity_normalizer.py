#!/usr/bin/env python3
"""Entity normalizer for cross-tool company/person name matching.

Normalizes names to a canonical form for matching across different data sources.
This is NOT for display - it's for matching entities from different tools.

Only stdlib: unicodedata, re.
stdout is reserved for the MCP protocol JSON — all logging goes to stderr.
"""

import os
import re
import sys
import unicodedata

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from common.structured_logging import get_logger

logger = get_logger(__name__, "common_entity_normalizer")

LEGAL_FORMS_PATTERN = re.compile(
    r'\b(?:S\.L\.U\.|S\.A\.|S\.L\.|sociedad\s+anonima|sociedad\s+limitada)(?=\W|$)',
    re.IGNORECASE,
)

PUNCTUATION_PATTERN = re.compile(r'[,.\-\n\r]')

SPACES_PATTERN = re.compile(r'\s+')


def normalize_for_match(name: str | None) -> str:
    r"""Normalize a company/person name for cross-tool matching.

    This produces a canonical form for matching purposes only, NOT for display.

    Steps:
    1. NFKD normalize (decompose accents)
    2. Strip combining characters (remove accents)
    3. Strip legal forms (S.A., S.L., S.L.U., "sociedad anonima", "sociedad limitada")
    4. Strip punctuation [,.\-\\n\\r]
    5. Collapse whitespace
    6. Lowercase

    Args:
        name: The name to normalize, or None.

    Returns:
        Normalized string, or "" if name is None/empty.
    """
    if name is None:
        return ""

    normalized = unicodedata.normalize('NFKD', name)
    normalized = ''.join(c for c in normalized if not unicodedata.combining(c))

    normalized = LEGAL_FORMS_PATTERN.sub('', normalized)

    normalized = PUNCTUATION_PATTERN.sub('', normalized)

    normalized = SPACES_PATTERN.sub(' ', normalized)

    normalized = normalized.strip().lower()

    return normalized
