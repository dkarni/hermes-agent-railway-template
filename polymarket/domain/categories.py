"""Category normalization helpers shared by profiling, scoring, and UI queries."""

from __future__ import annotations

UNKNOWN_CATEGORY = "UNKNOWN"

_MISSING_CATEGORY_VALUES = {
    "",
    UNKNOWN_CATEGORY,
    "UNCATEGORIZED",
    "N/A",
    "NONE",
    "NULL",
}


def canonical_category(value: object) -> str:
    """Return the canonical category key, or empty string when category is absent."""
    text = str(value or "").strip()
    if not text:
        return ""
    key = text.upper()
    return "" if key in _MISSING_CATEGORY_VALUES else key


def is_known_category(value: object) -> bool:
    return bool(canonical_category(value))
