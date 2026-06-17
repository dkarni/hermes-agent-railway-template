from __future__ import annotations

import json
import os
import secrets
from pathlib import Path


HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/data/.hermes"))
PROFILE = HERMES_HOME / "profiles" / "marco"
MCP_DIR = PROFILE / "mcp"
SKILL_DIR = PROFILE / "skills" / "logistics-ops"
BOOTSTRAP_FILES = Path("/app/bootstrap_files/mcp")

# Webhook token MUST be provided via SEVENTEENTRACK_WEBHOOK_TOKEN env var (set in Railway Variables).
# No hardcoded fallback so the secret is never committed to the repo.
WEBHOOK_TOKEN = ""


CONFIG_YAML = """mcp_servers:
  17track:
    command: "python"
    args:
      - "/data/.hermes/profiles/marco/mcp/17track_server.py"
    timeout: 120
    connect_timeout: 30
    tools:
      prompts: false
      resources: false
  selro:
    command: "python"
    args:
      - "/data/.hermes/profiles/marco/mcp/selro_server.py"
    timeout: 120
    connect_timeout: 30
    tools:
      prompts: false
      resources: false
  hubspot_email:
    command: "python"
    args:
      - "/data/.hermes/profiles/marco/mcp/hubspot_email_mcp.py"
    timeout: 120
    connect_timeout: 30
    tools:
      prompts: false
      resources: false
  woocommerce:
    command: "python"
    args:
      - "/data/.hermes/profiles/marco/mcp/woocommerce_server.py"
    timeout: 120
    connect_timeout: 30
    tools:
      prompts: false
      resources: false

platforms:
  webhook:
    enabled: true
    extra:
      host: "0.0.0.0"
      port: 8644
"""


SKILL_MD = """# Marco Logistics Ops

Marco handles logistics and shipping operations for Ethnic Musical.

## Rules

- Store every 17TRACK webhook update before deciding whether to alert.
- Alert Daniel only for important shipment events: delivered, failed delivery, exception, customs issue, returned to sender, out for delivery, pickup ready, expired, or other statuses needing attention.
- For routine in-transit and info-received updates, store the update and keep the reply internal.
- Do not send customer, carrier, or external messages without Daniel's explicit approval.

## 17TRACK Webhooks

When a 17TRACK webhook arrives, call `mcp_17track_store_webhook_update` with the full payload. Then inspect status and decide whether a concise Telegram alert to Daniel is warranted.
"""


BRIDGE_PY = r'''#!/usr/bin/env python3
"""Public 17TRACK webhook bridge for Marco.

Receives 17TRACK callbacks on /17track/<token>, signs the body with the
Hermes webhook HMAC secret, and forwards the event to the internal Hermes
webhook endpoint.
"""

import hashlib
import hmac
import json
import os
import uuid
from pathlib import Path

from aiohttp import ClientSession, web


PROFILE_ROOT = Path(__file__).resolve().parents[1]
SUBS_PATH = PROFILE_ROOT / "webhook_subscriptions.json"
ENV_PATHS = (PROFILE_ROOT / ".env", PROFILE_ROOT / "hermes.env")

FORWARD_URL = os.getenv("MARCO_17TRACK_FORWARD_URL", "http://127.0.0.1:8644/webhooks/17track")
HOST = os.getenv("MARCO_17TRACK_BRIDGE_HOST", "0.0.0.0")
PORT = int(os.getenv("MARCO_17TRACK_BRIDGE_PORT", "8654"))
MAX_BODY_BYTES = 1024 * 1024


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


def _token() -> str:
    token = os.getenv("SEVENTEENTRACK_WEBHOOK_TOKEN")
    if not token:
        raise RuntimeError("SEVENTEENTRACK_WEBHOOK_TOKEN is not configured")
    return token


def _hermes_secret() -> str:
    data = json.loads(SUBS_PATH.read_text(encoding="utf-8"))
    secret = data.get("17track", {}).get("secret")
    if not secret:
        raise RuntimeError(f"Missing 17track secret in {SUBS_PATH}")
    return secret


async def health(_request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "forward_url": FORWARD_URL})


async def receive(request: web.Request) -> web.Response:
    if request.match_info["token"] != _token():
        return web.json_response({"error": "not found"}, status=404)

    body = await request.read()
    if len(body) > MAX_BODY_BYTES:
        return web.json_response({"error": "payload too large"}, status=413)

    signature = hmac.new(_hermes_secret().encode("utf-8"), body, hashlib.sha256).hexdigest()
    request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
    headers = {
        "Content-Type": request.headers.get("Content-Type", "application/json"),
        "X-Webhook-Signature": signature,
        "X-Request-ID": request_id,
    }

    async with ClientSession() as session:
        async with session.post(FORWARD_URL, data=body, headers=headers) as response:
            text = await response.text()
            if response.status >= 400:
                return web.json_response(
                    {"error": "forward failed", "status": response.status, "body": text[:500]},
                    status=502,
                )

    return web.json_response({"accepted": True, "request_id": request_id}, status=202)


def make_app() -> web.Application:
    _load_env()
    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_post("/17track/{token}", receive)
    return app


if __name__ == "__main__":
    web.run_app(make_app(), host=HOST, port=PORT)
'''


TRACK_SERVER_PY = r'''#!/usr/bin/env python3
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
'''


def write_if_missing(path: Path, content: str, mode: int | None = None) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    if mode is not None:
        try:
            os.chmod(path, mode)
        except OSError:
            pass


def copy_if_missing(source: Path, dest: Path) -> None:
    if dest.exists() or not source.exists():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(source.read_bytes())


def ensure_env_file() -> None:
    env_path = PROFILE / "hermes.env"
    existing = {}
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                key, _, value = line.partition("=")
                existing[key.strip()] = value.strip()

    defaults = {
        "SEVENTEENTRACK_WEBHOOK_TOKEN": os.environ.get("SEVENTEENTRACK_WEBHOOK_TOKEN", WEBHOOK_TOKEN),
        "MARCO_17TRACK_FORWARD_URL": os.environ.get("MARCO_17TRACK_FORWARD_URL", "http://127.0.0.1:8644/webhooks/17track"),
        "MARCO_17TRACK_BRIDGE_HOST": os.environ.get("MARCO_17TRACK_BRIDGE_HOST", "127.0.0.1"),
        "MARCO_17TRACK_BRIDGE_PORT": os.environ.get("MARCO_17TRACK_BRIDGE_PORT", "8654"),
    }

    for key in (
        "SEVENTEENTRACK_API_KEY",
        "SELRO_KEY",
        "SELRO_KEY_SECRET",
        "HUBSPOT_TOKEN",
        "HUBSPOT_INBOX_ID",
        "HUBSPOT_CHANNEL_ACCOUNT_ID",
        "HUBSPOT_SENDER_ACTOR_ID",
        "HUBSPOT_SENDERS_JSON",
        "WC_CONSUMER_KEY",
        "WC_CONSUMER_SECRET",
        "WC_BASE_URL",
    ):
        if os.environ.get(key):
            defaults[key] = os.environ[key]

    changed = False
    for key, value in defaults.items():
        if value and not existing.get(key):
            existing[key] = value
            changed = True

    if changed or not env_path.exists():
        env_path.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"{key}={value}" for key, value in sorted(existing.items()) if value]
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        try:
            os.chmod(env_path, 0o600)
        except OSError:
            pass


def ensure_subscription() -> None:
    path = PROFILE / "webhook_subscriptions.json"
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}

    entry = data.get("17track", {})
    entry.setdefault(
        "description",
        "Receive 17TRACK package status webhooks, store them through the 17TRACK MCP tool, and notify D only for important shipment events.",
    )
    entry.setdefault("events", [])
    entry.setdefault("secret", os.environ.get("MARCO_17TRACK_HERMES_SECRET") or secrets.token_urlsafe(32))
    entry.setdefault(
        "prompt",
        "17TRACK webhook payload received. First call mcp_17track_store_webhook_update with the full payload. Then inspect the package status/event. Send D a concise Telegram alert only if the event is important: delivered, failed delivery, exception, customs issue, returned to sender, out for delivery, pickup ready, expired, or no-action status that still needs attention. For routine in-transit/info-received updates, store the update and reply with no user-facing alert beyond a one-line internal note.",
    )
    entry.setdefault("skills", ["logistics-ops"])
    entry.setdefault("deliver", "telegram")
    entry.setdefault("created_at", datetime_now())
    data["17track"] = entry

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def datetime_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main() -> None:
    MCP_DIR.mkdir(parents=True, exist_ok=True)
    SKILL_DIR.mkdir(parents=True, exist_ok=True)

    write_if_missing(PROFILE / "config.yaml", CONFIG_YAML)
    write_if_missing(MCP_DIR / "17track_webhook_bridge.py", BRIDGE_PY, 0o755)
    write_if_missing(MCP_DIR / "17track_server.py", TRACK_SERVER_PY, 0o755)
    write_if_missing(SKILL_DIR / "SKILL.md", SKILL_MD)

    copy_if_missing(BOOTSTRAP_FILES / "selro_server.py", MCP_DIR / "selro_server.py")
    copy_if_missing(BOOTSTRAP_FILES / "hubspot_email_mcp.py", MCP_DIR / "hubspot_email_mcp.py")
    copy_if_missing(BOOTSTRAP_FILES / "woocommerce_server.py", MCP_DIR / "woocommerce_server.py")

    ensure_env_file()
    ensure_subscription()

    print(f"Marco profile ready at {PROFILE}")


if __name__ == "__main__":
    main()
