#!/usr/bin/env python3
"""17TRACK MCP tools for Marco logistics."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP


mcp = FastMCP("17track")

PROFILE_ROOT = Path(__file__).resolve().parents[1]
ENV_PATHS = (PROFILE_ROOT / ".env", PROFILE_ROOT / "hermes.env")
STORE_PATH = PROFILE_ROOT / "data" / "17track_webhook_updates.jsonl"
BASE_URL = os.environ.get("SEVENTEENTRACK_BASE_URL", "https://api.17track.net/track/v2.4").rstrip("/")


def _load_env() -> None:
    for env_path in ENV_PATHS:
        if not env_path.exists():
            continue
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _api_key() -> str:
    _load_env()
    key = os.environ.get("SEVENTEENTRACK_API_KEY") or os.environ.get("TRACK17_API_KEY")
    if not key:
        raise RuntimeError("SEVENTEENTRACK_API_KEY is not configured")
    return key


async def _post(path: str, payload: Any) -> Any:
    headers = {"17token": _api_key(), "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(f"{BASE_URL}/{path.lstrip('/')}", json=payload, headers=headers)
        response.raise_for_status()
        return response.json()


def _coerce_payload(payload: str | dict | list) -> Any:
    if isinstance(payload, (dict, list)):
        return payload
    return json.loads(payload)


@mcp.tool()
async def store_webhook_update(payload: str) -> str:
    """Store a complete 17TRACK webhook payload for audit and later review."""
    data = _coerce_payload(payload)
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "received_at": datetime.now(timezone.utc).isoformat(),
        "payload": data,
    }
    with STORE_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return "Stored 17TRACK webhook update."


@mcp.tool()
async def list_recent_webhook_updates(limit: int = 20) -> str:
    """List recently stored 17TRACK webhook updates."""
    if not STORE_PATH.exists():
        return "No 17TRACK webhook updates stored yet."
    limit = max(1, min(int(limit), 100))
    lines = STORE_PATH.read_text(encoding="utf-8").splitlines()[-limit:]
    return "\n".join(lines)


@mcp.tool()
async def health_check() -> str:
    """Check 17TRACK MCP configuration and local store state."""
    _load_env()
    return json.dumps(
        {
            "api_key_configured": bool(os.environ.get("SEVENTEENTRACK_API_KEY") or os.environ.get("TRACK17_API_KEY")),
            "webhook_token_configured": bool(os.environ.get("SEVENTEENTRACK_WEBHOOK_TOKEN")),
            "store_path": str(STORE_PATH),
            "stored_updates": len(STORE_PATH.read_text(encoding="utf-8").splitlines()) if STORE_PATH.exists() else 0,
        },
        indent=2,
    )


@mcp.tool()
async def register_tracking(tracking_number: str, carrier_code: str = "", order_number: str = "") -> str:
    """Register a tracking number with 17TRACK."""
    item: dict[str, str] = {"number": tracking_number}
    if carrier_code:
        item["carrier"] = carrier_code
    if order_number:
        item["order_no"] = order_number
    return json.dumps(await _post("register", [item]), ensure_ascii=False, indent=2)


@mcp.tool()
async def register_tracking_batch(items_json: str) -> str:
    """Register a batch of tracking items. Pass a JSON array using 17TRACK fields."""
    return json.dumps(await _post("register", _coerce_payload(items_json)), ensure_ascii=False, indent=2)


@mcp.tool()
async def get_tracking_info(tracking_numbers_json: str) -> str:
    """Get tracking info for one or more tracking numbers. Pass a JSON array of numbers or item objects."""
    return json.dumps(await _post("gettrackinfo", _coerce_payload(tracking_numbers_json)), ensure_ascii=False, indent=2)


@mcp.tool()
async def get_realtime_tracking_info(tracking_number: str, carrier_code: str = "") -> str:
    """Get real-time tracking info for a tracking number."""
    item: dict[str, str] = {"number": tracking_number}
    if carrier_code:
        item["carrier"] = carrier_code
    return json.dumps(await _post("getrealtime", [item]), ensure_ascii=False, indent=2)


@mcp.tool()
async def get_tracking_list(page_no: int = 1, page_size: int = 40) -> str:
    """List registered 17TRACK trackings."""
    payload = {"page_no": int(page_no), "page_size": min(max(int(page_size), 1), 100)}
    return json.dumps(await _post("gettracklist", payload), ensure_ascii=False, indent=2)


@mcp.tool()
async def change_tracking_info(items_json: str) -> str:
    """Change 17TRACK tracking metadata. Pass a JSON array using 17TRACK fields."""
    return json.dumps(await _post("changetrackinfo", _coerce_payload(items_json)), ensure_ascii=False, indent=2)


@mcp.tool()
async def call_17track_endpoint(endpoint: str, payload_json: str) -> str:
    """Call a 17TRACK v2.4 endpoint with a raw JSON payload."""
    return json.dumps(await _post(endpoint, _coerce_payload(payload_json)), ensure_ascii=False, indent=2)


if __name__ == "__main__":
    _load_env()
    mcp.run()
