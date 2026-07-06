"""CSV serialization of list responses (?format=csv on list endpoints)."""

from __future__ import annotations

import csv
import io


def to_csv(items: list[dict]) -> str:
    """Flatten a list of dicts into CSV. Nested dicts/lists are JSON-encoded."""
    import json

    if not items:
        return ""
    # Stable column order: union of keys in first-seen order.
    columns: list[str] = []
    for item in items:
        for key in item:
            if key not in columns:
                columns.append(key)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for item in items:
        row = {}
        for key in columns:
            value = item.get(key)
            if isinstance(value, (dict, list)):
                value = json.dumps(value, separators=(",", ":"))
            row[key] = value
        writer.writerow(row)
    return buf.getvalue()
