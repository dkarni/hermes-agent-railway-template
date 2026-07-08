"""bootstrap_poly.py tests: create-when-absent, merge-preserving, idempotent,
parse-error-untouched — all against a tmp HERMES_HOME (dedicated profile)."""

from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_bootstrap(tmp_home: Path):
    """Import bootstrap_poly with paths repointed at the tmp home + repo files."""
    import sys

    sys.path.insert(0, str(REPO_ROOT))
    os.environ["HERMES_HOME"] = str(tmp_home)
    bp = importlib.import_module("bootstrap_poly")
    importlib.reload(bp)
    profile = tmp_home / "profiles" / "polymarket"
    bp.HERMES_HOME = tmp_home
    bp.PROFILE = profile
    bp.MCP_DIR = profile / "mcp"
    bp.SKILL_DIR = profile / "skills" / "polymarket-research"
    bp.CONFIG_YAML = profile / "config.yaml"
    bp.BOOTSTRAP_MCP = REPO_ROOT / "bootstrap_files" / "mcp"
    bp.BOOTSTRAP_POLY = REPO_ROOT / "bootstrap_files" / "poly"
    return bp


def test_creates_config_when_absent(tmp_path):
    bp = _load_bootstrap(tmp_path)
    bp.main()
    cfg = yaml.safe_load(bp.CONFIG_YAML.read_text())
    assert "polymarket" in cfg["mcp_servers"]
    assert cfg["mcp_servers"]["polymarket"]["args"] == [bp.MCP_SERVER_PATH]
    assert (bp.MCP_DIR / "polymarket_server.py").exists()
    assert (bp.SKILL_DIR / "SKILL.md").exists()


def test_merges_without_clobbering(tmp_path):
    bp = _load_bootstrap(tmp_path)
    bp.PROFILE.mkdir(parents=True, exist_ok=True)
    # Marco-style hand-configured profile with platform + engine settings.
    user_cfg = {
        "engine": {"provider": "openrouter", "model": "x"},
        "platforms": {"telegram": {"bot_token": "SECRET-TOKEN", "allowed_users": [111]}},
        "mcp_servers": {"existing": {"command": "python", "args": ["/x.py"]}},
    }
    bp.CONFIG_YAML.write_text(yaml.safe_dump(user_cfg, sort_keys=False))
    bp.main()
    cfg = yaml.safe_load(bp.CONFIG_YAML.read_text())
    assert cfg["platforms"]["telegram"]["bot_token"] == "SECRET-TOKEN"
    assert cfg["engine"]["model"] == "x"
    assert "existing" in cfg["mcp_servers"]
    assert "polymarket" in cfg["mcp_servers"]


def test_idempotent_second_run(tmp_path):
    bp = _load_bootstrap(tmp_path)
    bp.main()
    first = bp.CONFIG_YAML.read_text()
    bp.main()
    assert bp.CONFIG_YAML.read_text() == first


def test_parse_error_leaves_file_untouched(tmp_path, capsys):
    bp = _load_bootstrap(tmp_path)
    bp.PROFILE.mkdir(parents=True, exist_ok=True)
    broken = "this: : : [unbalanced\n"
    bp.CONFIG_YAML.write_text(broken)
    bp.main()
    assert bp.CONFIG_YAML.read_text() == broken
    assert "did not parse" in capsys.readouterr().out
