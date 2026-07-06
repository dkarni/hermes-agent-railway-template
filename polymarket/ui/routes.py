"""Server-rendered dashboard (Jinja2 + Tailwind CDN + Alpine) — PRD sec 20.

Served by the worker on loopback; server.py's proxy forwards /polymarket/* here
verbatim behind basic auth. Autoescape is ON (Jinja2 select_autoescape) so all
external strings — wallet labels, market questions, error text — are escaped.

Every page carries the global header (PAPER TRADING ONLY badge, active rule
version, data-freshness line, nav) via the shared context in _base_context.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, pass_context, select_autoescape
from starlette.requests import Request
from starlette.responses import HTMLResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .. import queries

_TEMPLATES = os.path.join(os.path.dirname(__file__), "templates")
_env = Environment(
    loader=FileSystemLoader(_TEMPLATES),
    autoescape=select_autoescape(["html", "xml"]),
    enable_async=True,
)


_EXTRA_FRACTION_RE = re.compile(r"\.(\d{6})\d+(?=(?:Z|[+-]\d\d:?\d\d)?$)")


def _parse_time(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value).strip()
        if not raw or raw in {"—", "never", "none", "None"}:
            return None
        raw = _EXTRA_FRACTION_RE.sub(r".\1", raw)
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _display_tz(context) -> ZoneInfo:
    tz = context.get("display_tz")
    if isinstance(tz, ZoneInfo):
        return tz
    try:
        return ZoneInfo(str(tz))
    except Exception:
        return ZoneInfo("UTC")


def _format_delta(seconds: int) -> str:
    if seconds < 45:
        return "just now"
    if seconds < 3600:
        minutes = max(1, round(seconds / 60))
        return f"{minutes} min ago"
    if seconds < 86400:
        hours = max(1, round(seconds / 3600))
        return f"{hours} hr ago"
    days = max(1, round(seconds / 86400))
    return f"{days} days ago"


@pass_context
def _relative_time(context, value, label: str | None = None, empty: str = "never") -> str:
    dt = _parse_time(value)
    if dt is None:
        text = empty
    else:
        now = datetime.now(timezone.utc)
        delta = int((now - dt).total_seconds())
        if delta >= 0 and delta < 7 * 86400:
            text = _format_delta(delta)
        else:
            local = dt.astimezone(_display_tz(context))
            text = local.strftime("%b %-d, %H:%M")
    return f"{label} {text}" if label else text


@pass_context
def _calendar_time(context, value, empty: str = "never") -> str:
    dt = _parse_time(value)
    if dt is None:
        return empty
    tz = _display_tz(context)
    local = dt.astimezone(tz)
    today = datetime.now(timezone.utc).astimezone(tz).date()
    if local.date() == today:
        return local.strftime("Today, %H:%M")
    if local.date().toordinal() == today.toordinal() - 1:
        return local.strftime("Yesterday, %H:%M")
    return local.strftime("%b %-d, %H:%M")


def _money_amount(value, empty: str = "—") -> str:
    if value is None:
        return empty
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return str(value)
    return f"${amount:,.2f}"


_env.filters["relative_time"] = _relative_time
_env.filters["calendar_time"] = _calendar_time
_env.filters["money_amount"] = _money_amount


def _sparkline(series: list[dict], *, width=520, height=60) -> dict:
    """Build inline-SVG sparkline geometry from an equity series (no chart lib)."""
    points = [s for s in series if s.get("equity_usd") is not None]
    if len(points) < 2:
        return {"path": "", "width": width, "height": height, "empty": True}
    values = [Decimal(p["equity_usd"]) for p in points]
    lo, hi = min(values), max(values)
    span = hi - lo
    n = len(values)
    pad = Decimal(8)
    drawable_height = Decimal(height) - (pad * 2)
    coords: list[tuple[float, float]] = []
    for i, v in enumerate(values):
        x = (Decimal(i) / Decimal(n - 1)) * Decimal(width)
        if span == 0:
            y = Decimal(height) / Decimal(2)
        else:
            y = pad + (Decimal(1) - ((v - lo) / span)) * drawable_height
        coords.append((float(x), float(y)))

    def pt(i: int) -> tuple[float, float]:
        return coords[min(max(i, 0), len(coords) - 1)]

    path = f"M {coords[0][0]:.1f},{coords[0][1]:.1f}"
    for i in range(len(coords) - 1):
        x0, y0 = pt(i - 1)
        x1, y1 = pt(i)
        x2, y2 = pt(i + 1)
        x3, y3 = pt(i + 2)
        c1x = x1 + (x2 - x0) / 6
        c1y = y1 + (y2 - y0) / 6
        c2x = x2 - (x3 - x1) / 6
        c2y = y2 - (y3 - y1) / 6
        path += f" C {c1x:.1f},{c1y:.1f} {c2x:.1f},{c2y:.1f} {x2:.1f},{y2:.1f}"

    baseline = height - 1
    area_path = (
        f"M {coords[0][0]:.1f},{baseline:.1f} "
        f"L {coords[0][0]:.1f},{coords[0][1]:.1f} "
        f"{path[2:]} "
        f"L {coords[-1][0]:.1f},{baseline:.1f} Z"
    )
    return {"path": path, "area_path": area_path, "width": width, "height": height, "empty": False,
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
        "display_tz": config.report_tz,
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
        conn, {"status": "track", "sort": "paper_pnl", "limit": "10"}
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
        Mount("/polymarket/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="poly-static"),
    ]
