"""MCP server checks: tool registry contains the expected names (no network).

`mcp` may not be installed in the test venv; the import test is skipped in that
case, but the AST-based tool-name check always runs (it needs no dependencies).
"""

from __future__ import annotations

import ast
import importlib.util
import os
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[2] / "bootstrap_files" / "mcp" / "polymarket_server.py"

EXPECTED_TOOLS = {
    "poly_overview", "poly_wallets", "poly_wallet", "poly_signals", "poly_signal",
    "poly_paper_trades", "poly_performance", "poly_benchmarks", "poly_rules",
    "poly_report", "poly_health", "poly_job_runs", "poly_run_job", "poly_rollback_rule",
}


def _tool_names() -> set[str]:
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for dec in node.decorator_list:
                if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                        and dec.func.attr == "tool"):
                    names.add(node.name)
    return names


def test_expected_tool_names_present():
    assert EXPECTED_TOOLS <= _tool_names()


def test_no_credential_tools():
    """No tool may mention keys/credentials in its name."""
    for name in _tool_names():
        assert "key" not in name.lower()
        assert "secret" not in name.lower()
        assert "credential" not in name.lower()


def test_action_allowlist_matches_prd():
    """poly_run_job's ACTION_NAMES set matches the PRD sec 19.2 action names."""
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    found = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "ACTION_NAMES":
                    found = {el.value for el in node.value.elts}
    assert found == {
        "scan-leaderboard", "ingest-history", "run-monitor", "profile-wallets",
        "reconcile-trades", "update-pnl", "review-outcomes", "evaluate-rules",
        "generate-report", "generate-weekly-report", "health-check", "reset-portfolio",
    }


@pytest.mark.skipif(importlib.util.find_spec("mcp") is None, reason="mcp not installed")
def test_module_imports_and_registers(monkeypatch):
    monkeypatch.setenv("POLY_API_URL", "http://127.0.0.1:8700")
    spec = importlib.util.spec_from_file_location("polymarket_server", SERVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.mcp.name == "polymarket"
