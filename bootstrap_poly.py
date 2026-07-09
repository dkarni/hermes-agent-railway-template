"""Bootstrap the dedicated `polymarket` Hermes profile (owner decision 2026-07-06).

Mirrors bootstrap_max.py, but the profile already exists on the Railway volume
and is hand-configured by the owner (its own Telegram bot + LLM engine), so
config.yaml is MERGED (add mcp_servers.polymarket only if absent) rather than
overwritten. The main /data/.hermes/config.yaml is never touched.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/data/.hermes"))
PROFILE = HERMES_HOME / "profiles" / "polymarket"
MCP_DIR = PROFILE / "mcp"
SKILL_DIR = PROFILE / "skills" / "polymarket-research"
CONFIG_YAML = PROFILE / "config.yaml"
BOOTSTRAP_MCP = Path("/app/bootstrap_files/mcp")
BOOTSTRAP_POLY = Path("/app/bootstrap_files/poly")

MCP_SERVER_PATH = "/data/.hermes/profiles/polymarket/mcp/polymarket_server.py"

MCP_ENTRY = {
    "command": "python",
    "args": [MCP_SERVER_PATH],
    "timeout": 120,
    "connect_timeout": 30,
    "tools": {"prompts": False, "resources": False},
}


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


def copy_always(source: Path, dest: Path, mode: int | None = None) -> None:
    """Overwrite dest from source when they differ. For CODE that must track the
    image (e.g. the MCP server), NOT for user-editable files. Without this, a
    stale copy on the /data volume shadows image updates forever — e.g. an old
    MCP client that omits the bearer token, yielding 401s against remote Poly.
    """
    if not source.exists():
        return
    if dest.exists() and dest.read_bytes() == source.read_bytes():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(source.read_bytes())
    if mode is not None:
        try:
            os.chmod(dest, mode)
        except OSError:
            pass


def merge_config(path: Path) -> None:
    """Add mcp_servers.polymarket to the profile config.yaml, preserving all else.

    - If the file is absent: create a minimal {mcp_servers: {polymarket: ...}}.
    - If present and parses: add the key only if 'polymarket' is absent, then
      write back with sort_keys=False (order preserved).
    - If present but fails to parse: print a warning and leave it untouched
      (never clobber a hand-configured file).
    """
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump({"mcp_servers": {"polymarket": MCP_ENTRY}}, sort_keys=False),
            encoding="utf-8",
        )
        print(f"Created {path} with mcp_servers.polymarket")
        return

    raw = path.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        print(f"WARNING: {path} did not parse as YAML ({exc}); leaving it untouched.")
        return

    if data is None:
        data = {}
    if not isinstance(data, dict):
        print(f"WARNING: {path} is not a YAML mapping; leaving it untouched.")
        return

    mcp_servers = data.get("mcp_servers")
    if mcp_servers is None:
        mcp_servers = {}
        data["mcp_servers"] = mcp_servers
    if not isinstance(mcp_servers, dict):
        print(f"WARNING: {path} mcp_servers is not a mapping; leaving it untouched.")
        return

    if "polymarket" in mcp_servers:
        print(f"{path} already has mcp_servers.polymarket; no change.")
        return

    mcp_servers["polymarket"] = MCP_ENTRY
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    print(f"Merged mcp_servers.polymarket into {path}")


def main() -> None:
    MCP_DIR.mkdir(parents=True, exist_ok=True)
    SKILL_DIR.mkdir(parents=True, exist_ok=True)

    # The MCP server is code: always refresh it so image updates (e.g. bearer-token
    # auth for remote Poly) reach the /data volume instead of being shadowed forever.
    copy_always(BOOTSTRAP_MCP / "polymarket_server.py", MCP_DIR / "polymarket_server.py", 0o755)
    copy_if_missing(BOOTSTRAP_POLY / "SOUL.md", PROFILE / "SOUL.md")
    write_if_missing(SKILL_DIR / "SKILL.md", (BOOTSTRAP_POLY / "SKILL.md").read_text(encoding="utf-8")
                     if (BOOTSTRAP_POLY / "SKILL.md").exists() else "# Polymarket Research Operator\n")
    merge_config(CONFIG_YAML)

    print(f"Polymarket profile ready at {PROFILE}")


if __name__ == "__main__":
    main()
