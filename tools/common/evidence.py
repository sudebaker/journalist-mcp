#!/usr/bin/env python3
"""Common Evidence model builder for OSINT tools.

Each OSINT tool's results[] array contains Evidence items built by
build_evidence(). The id field is a deterministic SHA-256 hash of
url + "|" + date + "|" + title, enabling deduplication across sources.
"""

import hashlib
from datetime import datetime, timezone

from common.structured_logging import get_logger

_logger = get_logger(__name__, "common_evidence")


def _compute_id(url: str, date: str, title: str) -> str:
    """Compute a deterministic 16-char evidence id.

    id = sha256(url + "|" + date + "|" + title)[:16]
    """
    raw = f"{url}|{date}|{title}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def build_evidence(
    source,
    official,
    confidence,
    title,
    date,
    url,
    metadata,
    query: str = "",
) -> dict:
    """Build a deterministic Evidence dict for OSINT tool results.

    Args:
        source: Source identifier/name.
        official: Whether the source is official/trusted.
        confidence: Confidence score (0.0 - 1.0).
        title: Evidence title/headline.
        date: Publication date string (ISO or similar).
        url: Canonical URL of the evidence.
        metadata: Dict of additional metadata.
        query: Original query string that produced this evidence.

    Returns:
        dict with fields: id, source, official, confidence, title,
        date, url, metadata, retrieved_at, query.
    """
    try:
        evidence_id = _compute_id(url, date, title)
    except Exception as e:
        _logger.error(
            "Failed to compute evidence id", extra_data={"error": str(e)}
        )
        evidence_id = ""

    return {
        "id": evidence_id,
        "source": source,
        "official": official,
        "confidence": confidence,
        "title": title,
        "date": date,
        "url": url,
        "metadata": metadata,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "query": query,
    }
