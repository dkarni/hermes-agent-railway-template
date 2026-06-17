# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp", "httpx"]
# ///
"""Selro MCP Server — orders, products, pricing, and stock."""

import os
from datetime import datetime
from typing import Any, Dict, List, Optional

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


mcp = FastMCP("selro")

BASE_URL = os.environ.get("SELRO_BASE_URL", "https://api.selro.com/3").rstrip("/")
AK = os.environ["SELRO_KEY"]
AS = os.environ["SELRO_KEY_SECRET"]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ap() -> Dict[str, str]:
    return {"key": AK, "secret": AS}


def _ts(t) -> str:
    if t is None:
        return "N/A"
    try:
        return datetime.fromtimestamp(t / 1000).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(t)


async def _get(ep: str, params: Optional[Dict] = None) -> Any:
    p = _ap()
    if params:
        p.update(params)
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.get(f"{BASE_URL}/{ep}", params=p)
        r.raise_for_status()
        return r.json()


async def _put(ep: str, json: Dict) -> Any:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.put(f"{BASE_URL}/{ep}", params=_ap(), json=json)
        r.raise_for_status()
        ct = r.headers.get("content-type", "")
        if "application/json" in ct:
            return r.json()
        return {"status_code": r.status_code, "text": r.text}


def _fmt_order(o: Dict) -> str:
    lines = [
        f"Selro ID: {o.get('id')}",
        f"Order ID: {o.get('orderId')}",
        f"Channel: {o.get('channel', 'N/A')}",
        f"Status: {o.get('orderStatus', 'N/A')}",
        f"Purchase: {_ts(o.get('purchaseDate'))}",
        f"Dispatch: {_ts(o.get('dispatchDate'))}",
        "",
        f"Customer: {o.get('buyerName', 'N/A')}",
        f"Email: {o.get('buyerEmail', 'N/A')}",
        f"Ship to: {o.get('shipName', '')}, {o.get('shipAddress1', '')}, "
        f"{o.get('shipCity', '')}, {o.get('shipCountryCode', '')}",
        "",
        f"Carrier: {o.get('carrierName', 'N/A')}",
        f"Method: {o.get('shippingMethod', 'N/A')}",
        f"Tracking: {o.get('trackingNumber', 'N/A')}",
        "",
        f"Total: {o.get('currencyCode', '')}{o.get('totalPrice', 'N/A')}",
        f"IOSS: {o.get('ioss', 'N/A')}",
    ]
    for sd in o.get("shippingDetails", []):
        lines.append(
            f"  Shipment: {sd.get('carrierName', 'N/A')} "
            f"TN:{sd.get('trackingNumber', 'N/A')} "
            f"Shipped:{_ts(sd.get('shippedDate'))}"
        )
    if o.get("channelSales"):
        lines += ["", "Items:"]
        for i in o["channelSales"]:
            lines.append(
                f"  - {i.get('sku', 'N/A')} x{i.get('quantityPurchased', '?')}: "
                f"{i.get('title', 'N/A')} "
                f"({o.get('currencyCode', '')}{i.get('itemPrice', 'N/A')})"
            )
    return "\n".join(lines)


# ── Order tools ───────────────────────────────────────────────────────────────

@mcp.tool()
async def selro_get_order(order_id: str) -> str:
    """Look up a Selro order by order ID. Returns full details including items, shipping, and tracking."""
    try:
        d = await _get("order", {"order_id": order_id})
        orders = d.get("orders", [])
        if not orders:
            return f"No order found: {order_id}"
        return _fmt_order(orders[0])
    except httpx.HTTPStatusError as e:
        return f"Error {e.response.status_code}: {e.response.text}"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
async def selro_search_orders(
    status: str = "",
    channel: str = "",
    from_date: str = "",
    to_date: str = "",
    page: int = 0,
    page_size: int = 20,
) -> str:
    """Search Selro orders.

    Args:
        status: Filter by status — Shipped, Unshipped, etc.
        channel: Filter by channel — amazon, ebay, etsy, etc.
        from_date: Start date in YYYY-MM-DD format.
        to_date: End date in YYYY-MM-DD format.
        page: Page number (default 0).
        page_size: Results per page, max 100 (default 20).
    """
    try:
        p: Dict[str, Any] = {"page": page, "pagesize": min(page_size, 100)}
        if status:
            p["status"] = status
        if channel:
            p["channel"] = channel
        if from_date:
            p["from_date"] = from_date
        if to_date:
            p["to_date"] = to_date
        orders = (await _get("orders", p)).get("orders", [])
        if not orders:
            return "No orders found."
        rows = [f"Found {len(orders)} order(s):"]
        for o in orders:
            # Get TN from shippingDetails first (more reliable), fall back to top-level
            tn = o.get("trackingNumber")
            if not tn:
                for sd in o.get("shippingDetails", []):
                    if sd.get("trackingNumber"):
                        tn = sd["trackingNumber"]
                        break
            rows.append(
                f"  #{o.get('orderId','?')} | {o.get('channel','?')} | "
                f"{o.get('orderStatus','?')} | {_ts(o.get('purchaseDate'))} | "
                f"{o.get('buyerName','N/A')} | TN:{tn or 'None'}"
            )
        return "\n".join(rows)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
async def selro_get_order_by_tracking(
    tracking_number: str,
    from_date: str = "",
    to_date: str = "",
) -> str:
    """Find a Selro order by tracking number. Scans all orders page by page.

    Always pass from_date and to_date when possible — without them the search
    scans the entire order history which is slow.

    Args:
        tracking_number: The tracking number to search for.
        from_date: Start date (YYYY-MM-DD). Strongly recommended to speed up search.
        to_date: End date (YYYY-MM-DD). Strongly recommended to speed up search.
    """
    try:
        tn_lower = tracking_number.lower()
        p: Dict[str, Any] = {"pagesize": 25}
        if from_date:
            p["from_date"] = from_date
        if to_date:
            p["to_date"] = to_date
        page = 0
        matches = []
        while True:
            p["page"] = page
            orders = (await _get("orders", p)).get("orders", [])
            if not orders:
                break
            for o in orders:
                if (o.get("trackingNumber") and tn_lower in o["trackingNumber"].lower()):
                    matches.append(o)
                elif any(
                    tn_lower in (sd.get("trackingNumber") or "").lower()
                    for sd in o.get("shippingDetails", [])
                ):
                    matches.append(o)
            if matches:
                break
            page += 1
        if not matches:
            return f"No orders found with tracking number: {tracking_number}"
        return "\n---\n".join(_fmt_order(o) for o in matches)
    except Exception as e:
        return f"Error: {e}"


# ── Channel tools ─────────────────────────────────────────────────────────────

@mcp.tool()
async def selro_list_channels(enabled_only: bool = True) -> str:
    """List your Selro sales channels. Use this to find valid channel names before updating prices.

    Args:
        enabled_only: If True (default), only return enabled/active channels.
    """
    try:
        channels = (await _get("channels")).get("channels") or []
        if enabled_only:
            channels = [c for c in channels if bool(c.get("enable"))]
        if not channels:
            return "No channels found."
        rows = [f"{'ID':<8} {'Type':<12} Name"]
        rows.append("-" * 50)
        for c in channels:
            rows.append(
                f"{str(c.get('id','?')):<8} "
                f"{c.get('type','?'):<12} "
                f"{c.get('name','N/A')}"
            )
        return "\n".join(rows)
    except Exception as e:
        return f"Error: {e}"


# ── Pricing tools ─────────────────────────────────────────────────────────────
#
# Selro's PUT /price endpoint accepts a "priceUpdates" array.
# Each item can set a base price and/or channel-specific prices via "channelPrice".
#
# Payload shape:
# {
#   "priceUpdates": [
#     {
#       "sku": "SELRO-SKU",          ← your Selro product SKU
#       "price": 22.12,              ← optional: update base price too
#       "channelPrice": [
#         {
#           "sku": "CHANNEL-SKU",    ← listing SKU on that channel (may differ)
#           "channel": "ebay",       ← channel name e.g. ebay, amazon, etsy
#           "site": "uk",            ← site/marketplace e.g. uk, us, de (optional)
#           "listingPrice": 23.34    ← the price to set on this channel
#         }
#       ]
#     }
#   ]
# }

@mcp.tool()
async def selro_update_channel_price(
    sku: str,
    channel: str,
    listing_price: float,
    site: str = "",
    channel_sku: str = "",
    base_price: float = 0,
    dry_run: bool = False,
) -> str:
    """Update the listing price for a specific channel and optionally the base inventory price.

    Args:
        sku: Your Selro product SKU.
        channel: Channel name exactly as Selro knows it — e.g. 'ebay', 'amazon', 'etsy'.
                 Run selro_list_channels to see valid names.
        listing_price: The new price to set on this channel (must be > 0).
        site: Marketplace site — e.g. 'US', 'UK', 'DE'. Leave blank if you only have one site per channel.
        channel_sku: The listing SKU on that channel if it differs from your Selro SKU.
                     Leave blank to use the same SKU.
        base_price: If > 0, also updates the base inventory price in Selro.
        dry_run: If True, shows the payload without sending it.
    """
    if not sku or not sku.strip():
        return "Error: sku is required."
    if listing_price <= 0:
        return "Error: listing_price must be > 0."
    if not channel or not channel.strip():
        return "Error: channel is required."

    selro_sku = sku.strip()
    ch_sku = channel_sku.strip() if channel_sku.strip() else selro_sku

    channel_entry: Dict[str, Any] = {
        "sku": ch_sku,
        "channel": channel.strip(),
        "listingPrice": float(listing_price),
    }
    if site.strip():
        channel_entry["site"] = site.strip()

    price_update: Dict[str, Any] = {
        "sku": selro_sku,
        "channelPrice": [channel_entry],
    }
    if base_price > 0:
        price_update["price"] = float(base_price)

    payload = {"priceUpdates": [price_update]}

    if dry_run:
        import json as _json
        return f"DRY RUN — payload that would be sent:\n{_json.dumps(payload, indent=2)}"

    try:
        result = await _put("price", json=payload)
        lines = [
            f"✓ Price updated.",
            f"Selro SKU: {selro_sku}",
        ]
        if base_price > 0:
            lines.append(f"Base price: {base_price:.2f}")
        lines.append(
            f"Channel: {channel.strip()}"
            + (f" / {site.strip()}" if site.strip() else "")
            + f" = {listing_price:.2f}"
        )
        lines.append(f"Selro response: {result}")
        return "\n".join(lines)
    except httpx.HTTPStatusError as e:
        return f"Error {e.response.status_code}: {e.response.text}"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
async def selro_bulk_update_channel_prices(
    updates: str,
    dry_run: bool = False,
) -> str:
    """Update listing prices for multiple SKUs across any channels in a single API call.

    Each item specifies its own channel (and optional site), so you can mix channels
    in one call — e.g. update Amazon UK and eBay UK in the same request.

    Args:
        updates: JSON array of objects. Each object must have:
                   - sku (str): Selro product SKU
                   - channel (str): channel name e.g. 'ebay', 'amazon'
                   - listing_price (float): new price on that channel
                 Optional per item:
                   - site (str): marketplace site e.g. 'US', 'UK'
                   - channel_sku (str): listing SKU on that channel if different from sku
                   - base_price (float): if > 0, also updates the base inventory price in Selro
        dry_run: If True, shows the payload without sending it.

    Example updates value:
        '[
          {"sku": "DRESS-001", "channel": "ebay", "site": "UK", "listing_price": 29.99, "base_price": 25.00},
          {"sku": "DRESS-001", "channel": "amazon", "site": "UK", "listing_price": 34.99},
          {"sku": "DRUM-002",  "channel": "etsy",  "listing_price": 49.00}
        ]'
    """
    import json as _json

    try:
        items: List[Dict] = _json.loads(updates)
        if not isinstance(items, list) or not items:
            return "Error: updates must be a non-empty JSON array."
    except Exception as e:
        return f"Error parsing updates JSON: {e}"

    # Validate upfront
    errors = []
    for i, item in enumerate(items):
        if not isinstance(item.get("sku"), str) or not str(item["sku"]).strip():
            errors.append(f"Item {i}: missing 'sku'")
        if not isinstance(item.get("channel"), str) or not str(item["channel"]).strip():
            errors.append(f"Item {i}: missing 'channel'")
        lp = item.get("listing_price")
        if not isinstance(lp, (int, float)) or lp <= 0:
            errors.append(f"Item {i} (sku={item.get('sku')}): 'listing_price' must be > 0")
    if errors:
        return "Validation errors:\n" + "\n".join(f"  - {e}" for e in errors)

    # Group by Selro SKU so multiple channels for the same SKU go in one priceUpdates entry
    grouped: Dict[str, List[Dict]] = {}
    base_prices: Dict[str, float] = {}
    for item in items:
        selro_sku = str(item["sku"]).strip()
        ch_sku = str(item.get("channel_sku", "")).strip() or selro_sku
        channel_entry: Dict[str, Any] = {
            "sku": ch_sku,
            "channel": str(item["channel"]).strip(),
            "listingPrice": float(item["listing_price"]),
        }
        site = str(item.get("site", "")).strip()
        if site:
            channel_entry["site"] = site
        grouped.setdefault(selro_sku, []).append(channel_entry)
        bp = item.get("base_price")
        if isinstance(bp, (int, float)) and bp > 0:
            base_prices[selro_sku] = float(bp)

    price_updates = []
    for selro_sku, channel_entries in grouped.items():
        entry: Dict[str, Any] = {"sku": selro_sku, "channelPrice": channel_entries}
        if selro_sku in base_prices:
            entry["price"] = base_prices[selro_sku]
        price_updates.append(entry)
    payload = {"priceUpdates": price_updates}

    if dry_run:
        return f"DRY RUN — payload that would be sent:\n{_json.dumps(payload, indent=2)}"

    try:
        result = await _put("price", json=payload)
        lines = [f"✓ Bulk channel price update sent ({len(items)} entries, {len(price_updates)} SKUs)."]
        for pu in price_updates:
            for cp in pu["channelPrice"]:
                lines.append(
                    f"  {pu['sku']} → {cp['channel']}"
                    + (f"/{cp['site']}" if cp.get("site") else "")
                    + f" = {cp['listingPrice']:.2f}"
                )
        lines.append(f"Selro response: {result}")
        return "\n".join(lines)
    except httpx.HTTPStatusError as e:
        return f"Error {e.response.status_code}: {e.response.text}"
    except Exception as e:
        return f"Error: {e}"


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()