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
        return local.strftime("Today at %H:%M")
    if local.date().toordinal() == today.toordinal() - 1:
        return local.strftime("Yesterday at %H:%M")
    return local.strftime("%b %-d, %H:%M")


def _money_amount(value, empty: str = "—") -> str:
    if value is None:
        return empty
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return str(value)
    return f"${amount:,.2f}"


def _dedupe_alerts(items: list[dict]) -> list[dict]:
    deduped = []
    seen = set()
    for item in items:
        key = (str(item.get("severity") or ""), str(item.get("message") or ""))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


_env.filters["relative_time"] = _relative_time
_env.filters["calendar_time"] = _calendar_time
_env.filters["money_amount"] = _money_amount


def _chart_time_label(value, tz: ZoneInfo | str | None) -> str:
    dt = _parse_time(value)
    if dt is None:
        return "snapshot"
    try:
        zone = tz if isinstance(tz, ZoneInfo) else ZoneInfo(str(tz))
    except Exception:
        zone = ZoneInfo("UTC")
    return dt.astimezone(zone).strftime("%b %-d, %H:%M")


def _money_label(value: Decimal) -> str:
    return f"${value:,.2f}"


def _decimal_value(value) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _pct_label(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.1'))}%"


def _portfolio_split(data: dict) -> dict:
    total = _decimal_value(data.get("equity_usd"))
    cash = _decimal_value(data.get("cash_usd"))
    if total is None and cash is None:
        return {"empty": True}
    if total is None:
        total = cash or Decimal(0)
    if cash is None:
        cash = Decimal(0)
    open_value = total - cash
    if open_value < 0:
        open_value = Decimal(0)
    if total > 0:
        cash_pct = max(Decimal(0), min(Decimal(100), (cash / total) * Decimal(100)))
        open_pct = max(Decimal(0), min(Decimal(100), (open_value / total) * Decimal(100)))
    else:
        cash_pct = Decimal(0)
        open_pct = Decimal(0)
    return {
        "empty": False,
        "total_label": _money_label(total),
        "cash_label": _money_label(cash),
        "open_value_label": _money_label(open_value),
        "cash_pct": f"{cash_pct:.2f}",
        "open_pct": f"{open_pct:.2f}",
        "cash_pct_label": _pct_label(cash_pct),
        "open_pct_label": _pct_label(open_pct),
    }


def _equity_chart(series: list[dict], *, tz: ZoneInfo | str | None = None) -> dict:
    """Build Chart.js-ready equity labels and values."""
    points = [s for s in series if s.get("equity_usd") is not None]
    if len(points) < 2:
        return {"empty": True, "labels": [], "values": []}
    values = [Decimal(p["equity_usd"]) for p in points]
    lo, hi = min(values), max(values)
    mid = (lo + hi) / Decimal(2)
    return {"empty": False,
            "labels": [_chart_time_label(p.get("collected_at"), tz) for p in points],
            "values": [float(v) for v in values],
            "first": str(values[0]), "last": str(values[-1]),
            "first_label": _money_label(values[0]), "last_label": _money_label(values[-1]),
            "min_label": _money_label(lo), "mid_label": _money_label(mid), "max_label": _money_label(hi),
            "start_label": _chart_time_label(points[0].get("collected_at"), tz),
            "end_label": _chart_time_label(points[-1].get("collected_at"), tz)}


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
    recent = await queries.recent_alerts(conn, limit=8)
    alert_items = _dedupe_alerts(
        [{"severity": "notice", "message": message, "created_at": None} for message in warnings]
        + [
            {
                "severity": a.get("severity") or "alert",
                "message": a.get("message") or "Alert",
                "created_at": a.get("created_at"),
            }
            for a in recent
        ]
    )
    return {
        "active_nav": active,
        "active_rule_version": version,
        "freshness": freshness,
        "display_tz": config.report_tz,
        "warnings": warnings,
        "alert_items": alert_items,
        "alert_count": len(alert_items),
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
    data["equity_chart"] = _equity_chart(series, tz=config.report_tz)
    data["portfolio_split"] = _portfolio_split(data)
    top_wallets = (await queries.wallets(
        conn, {"status": "track", "sort": "paper_pnl", "limit": "10"}
    ))["items"]
    data["top_wallets"] = top_wallets
    return {"o": data}


async def _wallets(conn, config, request):
    cur = await conn.execute(
        "SELECT DISTINCT UPPER(TRIM(category)) FROM wallet_category_stats "
        "WHERE category IS NOT NULL AND TRIM(category) != '' AND UPPER(TRIM(category)) != 'UNKNOWN' "
        "ORDER BY 1"
    )
    categories = [row[0] for row in await cur.fetchall()]
    if not categories:
        categories = ["POLITICS", "CRYPTO", "SPORTS", "CULTURE", "BUSINESS", "TECH"]
    return {"result": await queries.wallets(conn, dict(request.query_params)), "categories": categories}


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
    data["equity_chart"] = _equity_chart(data["equity_series"], tz=config.report_tz)
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
