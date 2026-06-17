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
  google_sheets:
    command: "uvx"
    args:
      - "mcp-google-sheets@latest"
      - "--include-tools"
      - "find_in_spreadsheet"
      - "get_sheet_data"
    timeout: 120
    connect_timeout: 60
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


# 17track webhook bridge source lives in bootstrap_files/mcp/17track_webhook_bridge.py
# 17track MCP server source lives in bootstrap_files/mcp/17track_server.py
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


def copy_if_missing(source: Path, dest: Path, mode: int | None = None) -> None:
    if dest.exists() or not source.exists():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(source.read_bytes())
    if mode is not None:
        try:
            os.chmod(dest, mode)
        except OSError:
            pass


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
    copy_if_missing(BOOTSTRAP_FILES / "17track_webhook_bridge.py", MCP_DIR / "17track_webhook_bridge.py", 0o755)
    copy_if_missing(BOOTSTRAP_FILES / "17track_server.py", MCP_DIR / "17track_server.py", 0o755)
    write_if_missing(SKILL_DIR / "SKILL.md", SKILL_MD)

    copy_if_missing(BOOTSTRAP_FILES / "selro_server.py", MCP_DIR / "selro_server.py")
    copy_if_missing(BOOTSTRAP_FILES / "hubspot_email_mcp.py", MCP_DIR / "hubspot_email_mcp.py")
    copy_if_missing(BOOTSTRAP_FILES / "woocommerce_server.py", MCP_DIR / "woocommerce_server.py")

    ensure_env_file()
    ensure_subscription()

    print(f"Marco profile ready at {PROFILE}")


if __name__ == "__main__":
    main()
