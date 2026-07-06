# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp", "httpx"]
# ///
"""MCP server exposing access to the dashboard's PO / product / supplier / velocity API.

Read tools hit v1 endpoints. Write tools (currently: creating PO drafts) hit v2 endpoints.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

# === Auto-load env from profile env files (must run before os.environ reads) ===
from pathlib import Path as _Path
_PROFILE_ROOT = _Path(__file__).resolve().parents[1]
for _env_path in (_PROFILE_ROOT / ".env", _PROFILE_ROOT / "hermes.env"):
    if _env_path.exists():
        for _raw in _env_path.read_text(encoding="utf-8").splitlines():
            _line = _raw.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _key, _, _value = _line.partition("=")
            os.environ.setdefault(_key.strip(), _value.strip().strip('"').strip("'"))
# === End auto-load env ===

API_URL = os.environ.get("PO_API_URL", "http://localhost:8000/api").rstrip("/")
API_TOKEN = os.environ.get("PO_API_TOKEN")

if not API_TOKEN:
    raise RuntimeError("PO_API_TOKEN environment variable is required")

mcp = FastMCP("po")

client = httpx.Client(
    base_url=API_URL,
    headers={
        "Authorization": f"Bearer {API_TOKEN}",
        "Accept": "application/json",
    },
    timeout=30.0,
)


def _get(path: str, params: dict[str, Any] | None = None) -> dict:
    params = {k: v for k, v in (params or {}).items() if v is not None}
    response = client.get(path, params=params)
    if response.status_code >= 400:
        raise RuntimeError(f"{response.status_code} {response.reason_phrase}: {response.text[:500]}")
    return response.json()


def _post(path: str, json: dict[str, Any]) -> dict:
    response = client.post(path, json=json)
    if response.status_code >= 400:
        raise RuntimeError(f"{response.status_code} {response.reason_phrase}: {response.text[:500]}")
    return response.json()


@mcp.tool()
def list_suppliers() -> dict:
    """List all suppliers (id, name, currency). Use this to look up the supplier_id for other tools."""
    return _get("/suppliers")


@mcp.tool()
def list_products(
    supplier_id: int | None = None,
    search: str | None = None,
    sku: list[str] | None = None,
    low_stock: bool | None = None,
    low_stock_threshold: int | None = None,
    sort: str | None = None,
    direction: str | None = None,
    per_page: int | None = None,
    page: int | None = None,
) -> dict:
    """Primary tool for reorder planning. Returns one row per SKU with current stock (from Selro sync),
    incoming_quantity (sum of outstanding qty on open POs — draft/raised/partially_received), open_po_count,
    and supplier_ids. Archived products are excluded.

    Use low_stock=True with an optional low_stock_threshold (default 5) to surface SKUs where
    available_stock + incoming_quantity is below the threshold. Combine with get_sales_velocity to
    decide which SKUs need a new PO.

    Fields returned per product: id, sku, name, supplier_ids, stock, available_stock, reserved_stock,
    incoming_quantity, open_po_count, image_url.
    """
    params: dict[str, Any] = {
        "supplier_id": supplier_id,
        "search": search,
        "low_stock": 1 if low_stock else None,
        "low_stock_threshold": low_stock_threshold,
        "sort": sort,
        "direction": direction,
        "per_page": per_page,
        "page": page,
    }
    if sku:
        params["sku"] = sku  # httpx serialises list -> sku[]=... style repeated params
    return _get("/products", params)


@mcp.tool()
def list_purchase_orders(
    status: list[str] | None = None,
    supplier_id: int | None = None,
    search: str | None = None,
    sort: str | None = None,
    direction: str | None = None,
    per_page: int | None = None,
    page: int | None = None,
) -> dict:
    """List purchase orders. Default (no status filter) returns open POs: raised + partially_received.
    Pass status=["all"] to include every status. Valid statuses: draft, raised, partially_received,
    fully_received, closed. search matches PO id (`#123` or `123`) or item SKU (LIKE).
    Sort keys: id, total_quantity, total_value, total_value_usd, status, created_at.
    """
    params: dict[str, Any] = {
        "supplier_id": supplier_id,
        "search": search,
        "sort": sort,
        "direction": direction,
        "per_page": per_page,
        "page": page,
    }
    if status:
        params["status"] = status
    return _get("/purchase-orders", params)


@mcp.tool()
def get_purchase_order(id: int) -> dict:
    """Get a single purchase order with line items, supplier, payments, and payment summary.
    Each item includes outstanding_quantity (quantity - received_quantity) and current_stock
    (live Selro-synced stock) for cross-checking.
    """
    return _get(f"/purchase-orders/{id}")


@mcp.tool()
def get_sales_velocity(
    time_range: str | None = None,
    supplier_id: int | None = None,
    search: str | None = None,
    attention: bool | None = None,
    slow_stock: bool | None = None,
    slow_stock_threshold: int | None = None,
    sort: str | None = None,
    direction: str | None = None,
    per_page: int | None = None,
    page: int | None = None,
) -> dict:
    """Sales velocity per SKU over a time range. Returns units_sold_period, velocity_monthly,
    days_of_stock_left, stock_health_label. Use to decide reorder urgency after scanning list_products.
    time_range: 'max' (default), '7d', '30d', '90d', '365d', or 'custom' with start_date/end_date.
    """
    params: dict[str, Any] = {
        "time_range": time_range,
        "supplier_id": supplier_id,
        "search": search,
        "attention": 1 if attention else None,
        "slow_stock": 1 if slow_stock else None,
        "slow_stock_threshold": slow_stock_threshold,
        "sort": sort,
        "direction": direction,
        "per_page": per_page,
        "page": page,
    }
    return _get("/sales-velocity", params)


@mcp.tool()
def create_purchase_order_draft(
    supplier_id: int,
    currency: str,
    items: list[dict[str, Any]],
    comments: str | None = None,
) -> dict:
    """Create a new purchase order in DRAFT status. Drafts are never sent to suppliers —
    a human still has to open the dashboard and promote the PO to `raised`. Use this to
    pre-populate a reorder suggestion after analysing stock/velocity.

    Args:
        supplier_id: Supplier to order from (use list_suppliers to look up).
        currency: ISO 4217 3-letter code, must match one the dashboard supports
            (USD, EUR, GBP, CAD, AUD, AED, SAR, PLN, SEK, JPY, TRY, ILS, INR).
            The exchange rate to USD is resolved server-side at creation time.
        items: Non-empty list of line items. Each item requires:
            - sku (str): product SKU (preferred — SKUs are unique and you already
              have them from list_products). Alternatively pass `product_id` (int).
            - quantity (int, >= 1)
            - unit_cost (number, optional): DO NOT PASS unless the user has given
              you a confirmed price. Leave the field out and the API will auto-fill
              from the last-known supplier price (with currency conversion),
              matching the dashboard's auto-fill behaviour. Products with no price
              history default to 0 and a human will edit them before raising.
            Product name is derived from the product record automatically.
        comments: Optional free-text note saved on the PO.

    Returns the created PO with id, totals, and line items. Status is always `draft`.
    """
    payload: dict[str, Any] = {
        "supplier_id": supplier_id,
        "currency": currency,
        "items": items,
    }
    if comments is not None:
        payload["comments"] = comments
    return _post("/v2/purchase-orders", payload)


if __name__ == "__main__":
    mcp.run()
