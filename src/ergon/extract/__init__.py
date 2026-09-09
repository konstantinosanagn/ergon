"""Field extraction framework.

Each structured field (level, comp, yoe, sector) is produced by a ``FieldExtractor`` that
maps a lightweight ``ExtractInput`` to a value. Extractors are registered and run by the
enrichment step. Geo is handled by a per-location normalizer (``geo.py``) since it mutates
``Location`` objects rather than returning a single posting-level value.

This package is the seam where rules can later be swapped for trained models per field
(see docs/superpowers/specs/2026-06-16-field-extraction-nlp-design.md).
"""

from __future__ import annotations

from .base import (
    ExtractInput,
    FieldExtractor,
    get_extractor,
    input_from_job,
    iter_extractors,
    register_extractor,
)

__all__ = [
    "ExtractInput",
    "FieldExtractor",
    "register_extractor",
    "get_extractor",
    "iter_extractors",
    "input_from_job",
]
