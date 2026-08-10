#!/usr/bin/env python3
"""Common Evidence model builder for OSINT tools.

Each OSINT tool's results[] array contains Evidence items built by
build_evidence(). The id field is a deterministic SHA-256 hash of
url + "|" + date + "|" + title, enabling deduplication across sources.

Contract (Fase 4):
- ``build_evidence`` produces one canonical Evidence dict. Optional fields
  (description, entity, evidence_type, raw) are only emitted when provided,
  so legacy callers keep a stable shape.
- ``build_search_result`` produces the canonical ``structured_content``
  envelope for every *_search tool: {source, target, results, count,
  official, evidence_type, duration_ms}.
- Contract guarantees: ``count == len(results)``; ``results`` is always a
  list (empty on no matches); deterministic ids.
"""

import hashlib
from datetime import datetime, timezone

from common.structured_logging import get_logger

_logger = get_logger(__name__, "common_evidence")

DEFAULT_EVIDENCE_TYPE = "official_record"


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
    metadata=None,
    query: str = "",
    description: str = "",
    entity: str = "",
    evidence_type: str | None = None,
    raw: dict | None = None,
) -> dict:
    """Build a deterministic Evidence dict for OSINT tool results.

    Args:
        source: Source identifier/name.
        official: Whether the source is official/trusted.
        confidence: Confidence score (0.0 - 1.0).
        title: Evidence title/headline.
        date: Publication date string (ISO or similar).
        url: Canonical URL of the evidence.
        metadata: Dict of additional metadata (legacy field).
        query: Original query string that produced this evidence.
        description: Optional human-readable description.
        entity: Optional referenced entity (NIF/CIF/nombre).
        evidence_type: Semantic type (default ``official_record``).
        raw: Optional source-specific dict preserved verbatim.

    Returns:
        dict with fields: id, source, official, confidence, title,
        date, url, metadata, retrieved_at, query; plus description,
        entity, evidence_type, raw only when provided.
    """
    try:
        evidence_id = _compute_id(url, date, title)
    except Exception as e:
        _logger.error(
            "Failed to compute evidence id", extra_data={"error": str(e)}
        )
        evidence_id = ""

    ev = {
        "id": evidence_id,
        "source": source,
        "official": official,
        "confidence": confidence,
        "title": title,
        "date": date,
        "url": url,
        "metadata": metadata or {},
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "query": query,
    }
    if description:
        ev["description"] = description
    if entity:
        ev["entity"] = entity
    if evidence_type:
        ev["evidence_type"] = evidence_type
    if raw is not None:
        ev["raw"] = raw
    return ev


def build_search_result(
    source: str,
    target: str,
    results: list,
    official: bool = True,
    evidence_type: str = DEFAULT_EVIDENCE_TYPE,
    duration_ms: int | None = None,
) -> dict:
    """Build the canonical ``structured_content`` envelope for search tools.

    Args:
        source: Source identifier (e.g. "boe").
        target: The searched target.
        results: List of Evidence dicts (from build_evidence).
        official: Whether the source is official/trusted.
        evidence_type: Semantic type of the results.
        duration_ms: Optional wall-clock duration of the search in ms.

    Returns:
        dict with fields: source, target, results, count, official,
        evidence_type; plus duration_ms only when provided.
    """
    envelope = {
        "source": source,
        "target": target,
        "results": results or [],
        "count": len(results or []),
        "official": official,
        "evidence_type": evidence_type,
    }
    if duration_ms is not None:
        envelope["duration_ms"] = duration_ms
    return envelope