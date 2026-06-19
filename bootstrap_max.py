from __future__ import annotations

import os
from pathlib import Path


HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/data/.hermes"))
PROFILE = HERMES_HOME / "profiles" / "max"
MCP_DIR = PROFILE / "mcp"
SKILL_DIR = PROFILE / "skills" / "purchasing-ops"
BOOTSTRAP_FILES = Path("/app/bootstrap_files/mcp")
MAX_FILES = Path("/app/bootstrap_files/max")


# PO MCP reads PO_API_URL / PO_API_TOKEN from the profile's hermes.env via its built-in
# env loader (see po_server.py), so no `env:` passthrough block is needed here.
CONFIG_YAML = """mcp_servers:
  po:
    command: "python"
    args:
      - "/data/.hermes/profiles/max/mcp/po_server.py"
    timeout: 120
    connect_timeout: 30
    tools:
      prompts: false
      resources: false
"""


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

    defaults = {}
    for key in ("PO_API_URL", "PO_API_TOKEN"):
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


def main() -> None:
    MCP_DIR.mkdir(parents=True, exist_ok=True)
    SKILL_DIR.mkdir(parents=True, exist_ok=True)

    write_if_missing(PROFILE / "config.yaml", CONFIG_YAML)
    copy_if_missing(BOOTSTRAP_FILES / "po_server.py", MCP_DIR / "po_server.py", 0o755)
    copy_if_missing(MAX_FILES / "SOUL.md", PROFILE / "SOUL.md")
    copy_if_missing(MAX_FILES / "SKILL.md", SKILL_DIR / "SKILL.md")

    ensure_env_file()

    print(f"Max profile ready at {PROFILE}")


if __name__ == "__main__":
    main()
