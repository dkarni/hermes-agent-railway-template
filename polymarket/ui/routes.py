"""Server-rendered dashboard (Jinja2 + Tailwind CDN + Alpine) — PRD sec 20.

Served by the worker on loopback; server.py's proxy forwards /polymarket/* here
verbatim behind basic auth. Autoescape is ON (Jinja2 select_autoescape) so all
external strings — wallet labels, market questions, error text — are escaped.

Every page carries the global header (PAPER TRADING ONLY badge, active rule
version, data-freshness line, nav) via the shared context in _base_context.
"""

from __future__ import annotations

import os

from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.requests import Request
from starlette.responses import HTMLResponse
from starlette.routing import Route

from .. import queries

_TEMPLATES = os.path.join(os.path.dirname(__file__), "templates")
_env = Environment(
    loader=FileSystemLoader(_TEMPLATES),
    autoescape=select_autoescape(["html", "xml"]),
    enable_async=True,
)


def _sparkline(series: list[dict], *, width=520, height=60) -> dict:
    """Build inline-SVG sparkline geometry from an equity series (no chart lib)."""
    from decimal import Decimal

    points = [s for s in series if s.get("equity_usd") is not None]
    if len(points) < 2:
        return {"path": "", "width": width, "height": height, "empty": True}
    values = [Decimal(p["equity_usd"]) for p in points]
    lo, hi = min(values), max(values)
    span = (hi - lo) or Decimal(1)
    n = len(values)
    coords = []
    for i, v in enumerate(values):
        x = (Decimal(i) / Decimal(n - 1)) * Decimal(width)
        y = Decimal(height) - ((v - lo) / span) * Decimal(height)
        coords.append(f"{float(x):.1f},{float(y):.1f}")
    path = "M " + " L ".join(coords)
    return {"path": path, "width": width, "height": height, "empty": False,
            "first": str(values[0]), "last": str(values[-1])}


async def _base_context(conn, config, request: Request, active: str) -> dict:
    from .. import db as dbmod
    version = await dbmod.active_rule_set_version(conn)
    freshness = await queries.data_freshness(conn)
    warnings = []
    if freshness.get("unresolved_critical_events"):
        warnings.append(
            f"{freshness['unresolved_critical_events']} unresolved critical data-quality event(s)."
        )
    if freshness.get("partial_scans"):
        warnings.append(f"{freshness['partial_scans']} partial leaderboard scan(s) on record.")
    if freshness.get("stale_marks"):
        warnings.append(f"{freshness['stale_marks']} open position(s) have a stale price mark.")
    return {
        "active_nav": active,
        "active_rule_version": version,
        "freshness": freshness,
        "warnings": warnings,
        "query": dict(request.query_params),
        "path": request.url.path,
    }


def _page(conn, config, template: str, active: str, builder):
    """Return a Starlette handler that renders ``template`` with page data."""

    async def handler(request: Request) -> HTMLResponse:
        context = await _base_context(conn, config, request, active)
        data = await builder(conn, config, request)
        if data is None:
            tmpl = _env.get_template("not_found.html")
            html = await tmpl.render_async(**context)
            return HTMLResponse(html, status_code=404)
        context.update(data)
        tmpl = _env.get_template(template)
        html = await tmpl.render_async(**context)
        return HTMLResponse(html)

    return handler


# --- page builders ----------------------------------------------------------

async def _overview(conn, config, request):
    data = await queries.overview(conn, config)
    from .. import queries as q
    from ..jobs.portfolio_view import get_active_portfolio_id
    pid = await get_active_portfolio_id(conn)
    series = await q.equity_sparkline(conn, pid)
    data["sparkline"] = _sparkline(series)
    top_wallets = (await queries.wallets(
        conn, {"status": "track", "sort": "score", "limit": "10"}
    ))["items"]
    data["top_wallets"] = top_wallets
    return {"o": data}


async def _wallets(conn, config, request):
    return {"result": await queries.wallets(conn, dict(request.query_params))}


async def _wallet_detail(conn, config, request):
    data = await queries.wallet_detail(conn, request.path_params["address"])
    if data is None:
        return None
    trades = await queries.wallet_trades(
        conn, request.path_params["address"], {"limit": "25"}
    )
    return {"w": data, "trades": trades}


async def _signals(conn, config, request):
    return {"result": await queries.signals(conn, dict(request.query_params))}


async def _paper_trades(conn, config, request):
    return {"result": await queries.paper_trades(conn, dict(request.query_params))}


async def _journal(conn, config, request):
    return {"result": await queries.journal(conn, dict(request.query_params))}


async def _performance(conn, config, request):
    window = dict(request.query_params).get("window")
    data = await queries.performance(conn, window)
    data["sparkline"] = _sparkline(data["equity_series"])
    return {"p": data}


async def _rules(conn, config, request):
    return {"r": await queries.rules(conn)}


async def _reports(conn, config, request):
    result = await queries.reports(conn, dict(request.query_params))
    detail = None
    date = dict(request.query_params).get("date")
    if date:
        detail = await queries.report_detail(
            conn, date, report_type=dict(request.query_params).get("type", "daily")
        )
    return {"result": result, "detail": detail}


async def _health(conn, config, request):
    return {"ops": await queries.health_ops(conn)}


def ui_routes(conn, config) -> list[Route]:
    """Dashboard routes; conn/config are captured in each page handler closure."""
    def p(template, active, builder):
        return _page(conn, config, template, active, builder)

    return [
        Route("/polymarket", p("overview.html", "overview", _overview)),
        Route("/polymarket/wallets", p("wallets.html", "wallets", _wallets)),
        Route("/polymarket/wallets/{address}", p("wallet_detail.html", "wallets", _wallet_detail)),
        Route("/polymarket/signals", p("signals.html", "signals", _signals)),
        Route("/polymarket/paper-trades", p("paper_trades.html", "paper-trades", _paper_trades)),
        Route("/polymarket/journal", p("journal.html", "journal", _journal)),
        Route("/polymarket/performance", p("performance.html", "performance", _performance)),
        Route("/polymarket/rules", p("rules.html", "rules", _rules)),
        Route("/polymarket/reports", p("reports.html", "reports", _reports)),
        Route("/polymarket/health", p("health.html", "health", _health)),
    ]
