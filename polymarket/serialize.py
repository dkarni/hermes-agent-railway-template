"""Shared JSON serialization helpers for the API and UI layers.

Convention (task spec / PRD sec 19): money and prices are returned as decimal
STRINGS in USD, timestamps are ISO-8601 UTC as stored, and demo rows carry an
``is_demo`` boolean flag. Micro-unit integers from the DB are converted at the
boundary via ``micro_to_usd`` / ``micro_to_px``.
"""

from __future__ import annotations

import json
from decimal import Decimal

from . import db as dbmod


def money(micro: int | None) -> str | None:
    """Micro USD integer -> decimal USD string (or None)."""
    if micro is None:
        return None
    return str(dbmod.micro_to_usd(int(micro)))


def price(micro: int | None) -> str | None:
    """Micro price integer (0..1e6) -> decimal [0,1] string (or None)."""
    if micro is None:
        return None
    return str(dbmod.micro_to_px(int(micro)))


def ratio(micro: int | None) -> str | None:
    """Micro ratio integer (x1e6) -> decimal string (or None). Signed ok."""
    if micro is None:
        return None
    return str((Decimal(int(micro)) / dbmod.MICRO).quantize(Decimal("0.000001")))


def dec(value: Decimal | None) -> str | None:
    """A Decimal (already USD/ratio) -> string (or None)."""
    if value is None:
        return None
    return str(value)


def flag(value) -> bool:
    """0/1/None -> bool."""
    return bool(value)


def load_json(text: str | None):
    if not text:
        return None
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None
