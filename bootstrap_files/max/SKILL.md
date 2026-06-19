# Purchasing Ops

Weekly: scan every supplier, alert Daniel what to order. On demand: "what should we order from supplier X / SKU X" → work it out and propose.

## Tools (PO MCP)
`list_suppliers`, `list_products`, `list_purchase_orders`, `get_purchase_order`, `get_sales_velocity` (read); `create_purchase_order_draft` (draft only — never sent to suppliers, Daniel raises it on the dashboard). Stock is Selro-synced (~15 min lag). `incoming_quantity` on `list_products` already sums open-PO quantities, so it accounts for what's in flight.

## Two rules that matter most
1. **Check open POs before flagging.** Run `list_purchase_orders()`, build the set of SKUs already in flight, and drop anything already on a PO from the alert. Flagging items already in production wastes Daniel's time.
2. **OOS ≠ no demand.** Zero velocity on an out-of-stock item just means we had nothing to sell. Flag those for restock, not for killing. Only "in stock + zero velocity for 90+ days" is genuinely slow.

## Weekly scan
`list_products(low_stock=true)` → cross-check against open POs → for survivors pull `get_sales_velocity` to judge urgency → send Daniel a short list: what to order, from whom, why. One line per item.

## PO draft ("prepare PO for supplier X")
Gather (`list_products(supplier_id=X)`, `get_sales_velocity(supplier_id=X)`, last PO via `list_purchase_orders(supplier_id=X, status=["all"])` + `get_purchase_order`), propose a compact table in chat, wait for Daniel's explicit "OK", then `create_purchase_order_draft`. Each item is `{sku, quantity}` only — omit `unit_cost` so the dashboard auto-fills from the last PO (pass it only if Daniel gives a confirmed price). Currency is uppercase ISO 4217, one per PO. Confirm with the draft ID and https://dashboard.ethnicmusical.com/. Never promote to raised.
