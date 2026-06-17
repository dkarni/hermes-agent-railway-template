# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp", "httpx"]
# ///
"""WooCommerce MCP Server for Ethnic Musical"""
import os, json, httpx
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

mcp = FastMCP("woocommerce")
BASE_URL = os.environ.get("WC_BASE_URL", "https://www.ethnicmusical.com/wp-json/wc/v3")
CK = os.environ["WC_CONSUMER_KEY"]
CS = os.environ["WC_CONSUMER_SECRET"]
def _ap(): return {"consumer_key": CK, "consumer_secret": CS}
async def _get(ep, params=None):
    url = f"{BASE_URL}/{ep}"
    p = _ap()
    if params: p.update(params)
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(url, params=p); r.raise_for_status(); return r.json()
def _fmt(o):
    b, s = o.get("billing", {}), o.get("shipping", {})
    l = [f"Order #{o.get('id')}", f"Status: {o.get('status')}", f"Date: {o.get('date_created')}",
         f"Customer: {b.get('first_name','')} {b.get('last_name','')}", f"Email: {b.get('email','')}",
         f"Phone: {b.get('phone','')}", "",
         f"Billing: {b.get('address_1','')}, {b.get('city','')}, {b.get('postcode','')}, {b.get('country','')}",
         f"Shipping: {s.get('address_1','')}, {s.get('city','')}, {s.get('postcode','')}, {s.get('country','')}", "", "Items:"]
    for i in o.get("line_items", []): l.append(f"  - {i.get('sku','N/A')} x{i.get('quantity')}: {i.get('name')} ({o.get('currency','')} {i.get('total')})")
    l += ["", "Shipping method:"]
    for sh in o.get("shipping_lines", []): l.append(f"  - {sh.get('method_title','N/A')} ({o.get('currency','')} {sh.get('total','0')})")
    tf = False
    for m in o.get("meta_data", []):
        k = m.get("key", "")
        if any(kw in k.lower() for kw in ["track", "parcel", "ppwc", "shipment", "_wc_shipment"]):
            if not tf: l += ["", "Tracking / Shipment data:"]; tf = True
            v = m.get("value")
            l.append(f"  {k}: {json.dumps(v, indent=2) if isinstance(v, (dict, list)) else v}")
    if not tf: l += ["", "Tracking: No tracking metadata found."]
    l += ["", f"Total: {o.get('currency','')} {o.get('total','')}", f"Payment: {o.get('payment_method_title','N/A')}"]
    if o.get("customer_note"): l.append(f"Customer note: {o.get('customer_note')}")
    return "\n".join(l)
@mcp.tool()
async def wc_get_order(order_id: str) -> str:
    """Look up a WooCommerce order by order number. Returns full order details including items, shipping, tracking, and customer info."""
    try: return _fmt(await _get(f"orders/{order_id}"))
    except httpx.HTTPStatusError as e: return f"Error: {e.response.status_code} - {e.response.text}"
    except Exception as e: return f"Error: {e}"
@mcp.tool()
async def wc_search_orders(search: str, status: str = "", per_page: int = 10, after: str = "", before: str = "") -> str:
    """Search WooCommerce orders by customer name, email, or order details.
    Args:
        search: Customer name, email, or order detail to search for.
        status: Filter by order status (e.g. completed, processing). Optional.
        per_page: Number of results (max 100, default 10).
        after: Only orders after this date, ISO 8601 format (e.g. 2025-01-01T00:00:00). Optional.
        before: Only orders before this date, ISO 8601 format (e.g. 2025-03-01T00:00:00). Optional.
    """
    try:
        p = {"search": search, "per_page": min(per_page, 100)}
        if status: p["status"] = status
        if after: p["after"] = after
        if before: p["before"] = before
        orders = await _get("orders", p)
        if not orders: return f"No orders found for '{search}'"
        r = [f"Found {len(orders)} order(s):\n"]
        for o in orders:
            b = o.get("billing", {})
            r.append(f"  #{o['id']} | {o['status']} | {o['date_created'][:10]} | {b.get('first_name','')} {b.get('last_name','')} | {b.get('email','')} | {o.get('currency','')} {o.get('total','')}")
        return "\n".join(r)
    except Exception as e: return f"Error: {e}"
@mcp.tool()
async def wc_get_order_notes(order_id: str) -> str:
    """Get notes for a WooCommerce order."""
    try:
        notes = await _get(f"orders/{order_id}/notes")
        if not notes: return f"No notes for order #{order_id}"
        return "\n".join([f"Notes for #{order_id}:"] + [f"  [{n['date_created'][:16]}] ({'Customer' if n.get('customer_note') else 'Internal'}): {n.get('note','')}" for n in notes])
    except Exception as e: return f"Error: {e}"
@mcp.tool()
async def wc_get_customer_orders(email: str) -> str:
    """Look up all orders for a customer by email."""
    try:
        orders = await _get("orders", {"search": email, "per_page": 20})
        if not orders: return f"No orders for {email}"
        return "\n".join([f"Orders for {email} ({len(orders)}):"] + [f"  #{o['id']} | {o['status']} | {o['date_created'][:10]} | {o.get('currency','')} {o.get('total','')} | {', '.join(i.get('name','?') for i in o.get('line_items',[]))[:80]}" for o in orders])
    except Exception as e: return f"Error: {e}"
def _extract_tns(o):
    """Extract all tracking numbers from an order's meta_data."""
    tns = []
    for m in o.get("meta_data", []):
        k = m.get("key", "")
        if any(kw in k.lower() for kw in ["track", "parcel", "ppwc", "shipment", "_wc_shipment"]):
            v = m.get("value")
            if isinstance(v, str) and v.strip():
                tns.append(v.strip())
            elif isinstance(v, list):
                for item in v:
                    if isinstance(item, dict):
                        for fk in ("tracking_number", "trackingNumber", "number"):
                            if item.get(fk): tns.append(str(item[fk]).strip())
                    elif isinstance(item, str) and item.strip():
                        tns.append(item.strip())
            elif isinstance(v, dict):
                for fk in ("tracking_number", "trackingNumber", "number"):
                    if v.get(fk): tns.append(str(v[fk]).strip())
    return tns

@mcp.tool()
async def wc_get_order_by_tracking(tracking_number: str, after: str = "", before: str = "") -> str:
    """Find a WooCommerce order by tracking number. Searches order meta_data.
    Args:
        tracking_number: The tracking number to search for.
        after: Only orders after this date, ISO 8601 format (e.g. 2025-01-01T00:00:00). Optional but recommended.
        before: Only orders before this date, ISO 8601 format (e.g. 2025-03-01T00:00:00). Optional but recommended.
    """
    try:
        tn_lower = tracking_number.lower()
        p = {"per_page": 100, "orderby": "date", "order": "desc"}
        if after: p["after"] = after
        if before: p["before"] = before
        page = 1
        max_pages = 5 if (after or before) else 3
        while page <= max_pages:
            p["page"] = page
            orders = await _get("orders", p)
            if not orders:
                break
            for o in orders:
                tns = _extract_tns(o)
                if any(tn_lower in tn.lower() for tn in tns):
                    return _fmt(o)
            page += 1
        return f"No WooCommerce orders found with tracking number: {tracking_number}"
    except Exception as e: return f"Error: {e}"

if __name__ == "__main__": mcp.run()
