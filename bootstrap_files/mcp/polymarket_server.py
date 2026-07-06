# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp", "httpx"]
# ///
"""MCP server exposing the Polymarket paper-trading research dashboard API.

Thin HTTP wrappers over the loopback worker API (same container). PAPER TRADING
ONLY: no tool accepts or mentions wallet keys, credentials, or order-execution
instructions. Read tools return the dashboard's JSON; action tools trigger the
same deterministic jobs the scheduler runs and return a job-run id.

No secrets required — the worker is loopback and the server.py proxy is the auth
boundary. POLY_API_URL defaults to the in-container worker.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

# === Auto-load env from profile env files (optional; no secrets needed) ===
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

API_URL = os.environ.get("POLY_API_URL", "http://127.0.0.1:8700").rstrip("/")

# Allowlisted operator action names (PRD sec 19.2). No other action is callable.
ACTION_NAMES = {
    "scan-leaderboard", "profile-wallets", "reconcile-trades", "update-pnl",
    "review-outcomes", "evaluate-rules", "generate-report", "reset-portfolio",
}

mcp = FastMCP("polymarket")

client = httpx.Client(base_url=API_URL, headers={"Accept": "application/json"}, timeout=30.0)


def _get(path: str, params: dict[str, Any] | None = None) -> dict:
    params = {k: v for k, v in (params or {}).items() if v is not None}
    response = client.get(path, params=params)
    if response.status_code >= 400:
        raise RuntimeError(f"{response.status_code}: {response.text[:500]}")
    return response.json()


def _post(path: str) -> dict:
    response = client.post(path)
    if response.status_code == 409:
        return {"error": "job already running", "detail": response.json()}
    if response.status_code >= 400:
        raise RuntimeError(f"{response.status_code}: {response.text[:500]}")
    return response.json()


@mcp.tool()
def poly_overview() -> dict:
    """Morning review at a glance. Returns paper equity/PnL, filtered-vs-blind
    verdict (with sample caveat), today's move, open positions, top tracked
    wallets, recent rule changes, alerts, and data freshness. Start here."""
    return _get("/api/polymarket/overview")


@mcp.tool()
def poly_wallets(status: str | None = None, category: str | None = None,
                 min_score: int | None = None, limit: int = 20) -> dict:
    """List tracked/watched wallets ranked by copied paper-PnL contribution.
    Filter by status (track|watch|ignore), category, or min_score. Use to answer
    'which wallets are worth copying?'. Each row includes the wallet's own score
    plus its copied-paper performance."""
    return _get("/api/polymarket/wallets",
                {"status": status, "category": category, "min_score": min_score, "limit": limit})


@mcp.tool()
def poly_wallet(address: str) -> dict:
    """Full profile for one wallet: category performance, profit concentration,
    status history (upgrades/downgrades), and its paper-copy results."""
    return _get(f"/api/polymarket/wallets/{address}")


@mcp.tool()
def poly_signals(decision: str | None = None, since: str | None = None, limit: int = 20) -> dict:
    """Recent trade signals + decisions. Filter decision (paper_copy|watchlist|
    skip) or since (ISO ts). Use to answer 'why was X copied/skipped?' — combine
    with poly_signal for the full component breakdown."""
    return _get("/api/polymarket/signals",
                {"decision": decision, "since": since, "limit": limit})


@mcp.tool()
def poly_signal(id: int) -> dict:
    """Full explanation for one signal: component scores, reasons, risks, hard
    gates, and the data known at decision time. Use to explain a single call."""
    return _get(f"/api/polymarket/signals/{id}")


@mcp.tool()
def poly_paper_trades(status: str | None = None, limit: int = 20) -> dict:
    """Paper (simulated) trades. Filter status (open|closed|resolved). All PnL is
    paper; the blind cohort uses fixed $10 sizing with no quality filters."""
    return _get("/api/polymarket/paper-trades", {"status": status, "limit": limit})


@mcp.tool()
def poly_performance(window: str = "30d") -> dict:
    """Performance over a window (7d|30d|90d|all): equity curve, filtered-vs-blind,
    per-category / per-wallet / per-rule-version breakdowns, decision-quality labels."""
    return _get("/api/polymarket/performance", {"window": window})


@mcp.tool()
def poly_benchmarks() -> dict:
    """Filtered strategy vs blind copy. Answers 'are we beating blind copying?'
    Refuses to claim an edge below the minimum sample and says so."""
    return _get("/api/polymarket/performance/benchmarks")


@mcp.tool()
def poly_rules() -> dict:
    """Active rule set (thresholds + weights), version history, and the change
    log (before→after, evidence, outcome). Use to explain rule changes."""
    return _get("/api/polymarket/rules")


@mcp.tool()
def poly_report(date: str | None = None) -> dict:
    """Stored daily/weekly report. With date=YYYY-MM-DD returns that report;
    without, lists recent reports. Reports are dashboard-only (no delivery send)."""
    if date:
        return _get(f"/api/polymarket/reports/{date}")
    return _get("/api/polymarket/reports")


@mcp.tool()
def poly_health() -> dict:
    """Operational health: data freshness, last successful + failed jobs, stale
    profiles, partial scans, missing reviews, open data-quality events."""
    return _get("/api/polymarket/health")


@mcp.tool()
def poly_job_runs(limit: int = 20) -> dict:
    """Recent job runs with status, counts, and errors. Use to diagnose failures
    before deciding whether to retry."""
    return _get("/api/polymarket/job-runs", {"limit": limit})


@mcp.tool()
def poly_run_job(name: str) -> dict:
    """Trigger a deterministic operator job by name. Allowed names: scan-leaderboard,
    profile-wallets, reconcile-trades, update-pnl, review-outcomes, evaluate-rules,
    generate-report, reset-portfolio. Returns a job_run_id (or a 409 if already
    running). Never places real orders — paper mode only."""
    if name not in ACTION_NAMES:
        raise RuntimeError(f"unknown job '{name}'. Allowed: {sorted(ACTION_NAMES)}")
    return _post(f"/api/polymarket/actions/{name}")


@mcp.tool()
def poly_rollback_rule(version: int) -> dict:
    """Manually roll back the active rule version to its parent (admin action).
    Reuses the deterministic rollback path; do not use to override safeguards —
    only to revert a change you can justify from stored evidence (poly_rules)."""
    return _post(f"/api/polymarket/actions/rollback-rule/{version}")


if __name__ == "__main__":
    mcp.run()
