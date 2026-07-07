"""Internal Starlette app (loopback only) — PRD sec 19 API + dashboard mount.

server.py's authenticated proxy is the auth boundary; this app is served on
127.0.0.1 and must not be exposed directly. Money and prices are decimal STRINGS
in USD; timestamps are ISO UTC; every list carries a total count for pagination.
"""

from __future__ import annotations

import contextlib
from typing import Awaitable, Callable, Sequence

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

from . import actions as actionmod
from . import csv_export
from . import db as dbmod
from . import queries
from .ui.routes import ui_routes


def _params(request: Request) -> dict:
    return dict(request.query_params)


def _csv_response(items: list[dict], filename: str) -> Response:
    body = csv_export.to_csv(items)
    return Response(
        body,
        media_type="text/csv; charset=utf-8",
        headers={"content-disposition": f'attachment; filename="{filename}.csv"'},
    )


def _calibration_csv_rows(result: dict) -> list[dict]:
    """One row per component x band; component-level stats repeated per row."""
    rows: list[dict] = []
    for comp in result.get("components", []):
        for band in comp.get("bands", []):
            rows.append({
                "component": comp["component"],
                "band": band["band"],
                "n": band["n"],
                "bad_rate": band["bad_rate"],
                "avg_pnl_usd": band["avg_pnl_usd"],
                "auc": comp["auc"],
                "n_good": comp["n_good"],
                "n_bad": comp["n_bad"],
                "separation": comp["separation"],
                "sufficient": comp["sufficient"],
            })
    return rows


def create_app(
    conn,
    config,
    *,
    on_shutdown: Sequence[Callable[[], Awaitable[None]]] | None = None,
    scheduler=None,
) -> Starlette:
    shutdown_handlers = list(on_shutdown or [])

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        yield
        for handler in shutdown_handlers:
            await handler()

    # --- read routes (PRD 19.1) --------------------------------------------

    async def overview(_request: Request) -> JSONResponse:
        return JSONResponse(await queries.overview(conn, config))

    async def wallets(request: Request) -> Response:
        params = _params(request)
        result = await queries.wallets(conn, params)
        if params.get("format") == "csv":
            return _csv_response(result["items"], "wallets")
        return JSONResponse(result)

    async def wallet_detail(request: Request) -> JSONResponse:
        data = await queries.wallet_detail(conn, request.path_params["address"])
        if data is None:
            return JSONResponse({"error": "wallet not found"}, status_code=404)
        return JSONResponse(data)

    async def wallet_trades(request: Request) -> JSONResponse:
        return JSONResponse(
            await queries.wallet_trades(conn, request.path_params["address"], _params(request))
        )

    async def signals(_request: Request) -> JSONResponse:
        return JSONResponse(await queries.signals(conn, _params(_request)))

    async def signal_detail(request: Request) -> JSONResponse:
        data = await queries.signal_detail(conn, int(request.path_params["id"]))
        if data is None:
            return JSONResponse({"error": "signal not found"}, status_code=404)
        return JSONResponse(data)

    async def paper_trades(request: Request) -> Response:
        params = _params(request)
        result = await queries.paper_trades(conn, params)
        if params.get("format") == "csv":
            return _csv_response(result["items"], "paper-trades")
        return JSONResponse(result)

    async def paper_trade_detail(request: Request) -> JSONResponse:
        data = await queries.paper_trade_detail(conn, int(request.path_params["id"]))
        if data is None:
            return JSONResponse({"error": "paper trade not found"}, status_code=404)
        return JSONResponse(data)

    async def journal(request: Request) -> Response:
        params = _params(request)
        result = await queries.journal(conn, params)
        if params.get("format") == "csv":
            return _csv_response(result["items"], "journal")
        return JSONResponse(result)

    async def performance(request: Request) -> JSONResponse:
        return JSONResponse(await queries.performance(conn, _params(request).get("window")))

    async def performance_calibration(request: Request) -> Response:
        params = _params(request)
        result = await queries.calibration(conn, params.get("window"))
        if params.get("format") == "csv":
            return _csv_response(_calibration_csv_rows(result), "calibration")
        return JSONResponse(result)

    async def performance_benchmarks(_request: Request) -> JSONResponse:
        return JSONResponse(await queries.benchmarks(conn))

    async def rules(_request: Request) -> JSONResponse:
        return JSONResponse(await queries.rules(conn))

    async def rule_version(request: Request) -> JSONResponse:
        data = await queries.rule_version(conn, int(request.path_params["version"]))
        if data is None:
            return JSONResponse({"error": "rule version not found"}, status_code=404)
        return JSONResponse(data)

    async def reports(request: Request) -> JSONResponse:
        return JSONResponse(await queries.reports(conn, _params(request)))

    async def report_detail(request: Request) -> JSONResponse:
        params = _params(request)
        data = await queries.report_detail(
            conn, request.path_params["date"], report_type=params.get("type", "daily")
        )
        if data is None:
            return JSONResponse({"error": "report not found"}, status_code=404)
        return JSONResponse(data)

    async def job_runs(request: Request) -> JSONResponse:
        return JSONResponse(await queries.job_runs(conn, _params(request)))

    async def health(_request: Request) -> JSONResponse:
        migrations = await dbmod.applied_migrations(conn)
        version = await dbmod.active_rule_set_version(conn)
        portfolio = await _portfolio_health(conn)
        ops = await queries.health_ops(conn)
        return JSONResponse(
            {
                "status": "ok",
                "trading_mode": config.trading_mode,
                "db_path": config.db_path,
                "migrations_applied": migrations,
                "active_rule_set_version": version,
                "scheduler_jobs": scheduler.job_names() if scheduler is not None else [],
                "open_positions": portfolio["open_positions"],
                "equity_usd": portfolio["equity_usd"],
                "last_pnl_snapshot_at": portfolio["last_pnl_snapshot_at"],
                "ops": ops,
            }
        )

    # --- action routes (PRD 19.2) ------------------------------------------

    async def _run_action(action: str) -> JSONResponse:
        try:
            job_run_id = await actionmod.start_action(conn, config, action)
        except actionmod.ActionRunning as exc:
            return JSONResponse(
                {"error": "job already running", "job_name": exc.job_name,
                 "job_run_id": exc.job_run_id},
                status_code=409,
            )
        except actionmod.ActionNotFound:
            return JSONResponse({"error": "unknown action"}, status_code=404)
        return JSONResponse({"job_run_id": job_run_id}, status_code=202)

    def _action_handler(action: str):
        async def handler(_request: Request) -> JSONResponse:
            return await _run_action(action)
        return handler

    async def retry_job(request: Request) -> JSONResponse:
        try:
            result = await actionmod.retry_job(conn, config, int(request.path_params["id"]))
        except actionmod.ActionRunning as exc:
            return JSONResponse(
                {"error": "job already running", "job_name": exc.job_name,
                 "job_run_id": exc.job_run_id},
                status_code=409,
            )
        except actionmod.ActionNotFound as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        return JSONResponse(result, status_code=202)

    async def rollback_rule(request: Request) -> JSONResponse:
        try:
            result = await actionmod.rollback_rule(conn, int(request.path_params["version"]))
        except actionmod.ActionRunning as exc:
            return JSONResponse(
                {"error": "job already running", "job_name": exc.job_name,
                 "job_run_id": exc.job_run_id},
                status_code=409,
            )
        return JSONResponse(result)

    api = "/api/polymarket"
    routes = [
        Route(f"{api}/overview", overview, methods=["GET"]),
        Route(f"{api}/wallets", wallets, methods=["GET"]),
        Route(f"{api}/wallets/{{address}}/trades", wallet_trades, methods=["GET"]),
        Route(f"{api}/wallets/{{address}}", wallet_detail, methods=["GET"]),
        Route(f"{api}/signals", signals, methods=["GET"]),
        Route(f"{api}/signals/{{id}}", signal_detail, methods=["GET"]),
        Route(f"{api}/paper-trades", paper_trades, methods=["GET"]),
        Route(f"{api}/paper-trades/{{id}}", paper_trade_detail, methods=["GET"]),
        Route(f"{api}/journal", journal, methods=["GET"]),
        Route(f"{api}/performance/calibration", performance_calibration, methods=["GET"]),
        Route(f"{api}/performance/benchmarks", performance_benchmarks, methods=["GET"]),
        Route(f"{api}/performance", performance, methods=["GET"]),
        Route(f"{api}/rules/{{version}}", rule_version, methods=["GET"]),
        Route(f"{api}/rules", rules, methods=["GET"]),
        Route(f"{api}/reports/{{date}}", report_detail, methods=["GET"]),
        Route(f"{api}/reports", reports, methods=["GET"]),
        Route(f"{api}/health", health, methods=["GET"]),
        Route(f"{api}/job-runs", job_runs, methods=["GET"]),
        # Actions.
        Route(f"{api}/actions/scan-leaderboard", _action_handler("scan-leaderboard"), methods=["POST"]),
        Route(f"{api}/actions/ingest-history", _action_handler("ingest-history"), methods=["POST"]),
        Route(f"{api}/actions/run-monitor", _action_handler("run-monitor"), methods=["POST"]),
        Route(f"{api}/actions/profile-wallets", _action_handler("profile-wallets"), methods=["POST"]),
        Route(f"{api}/actions/reconcile-trades", _action_handler("reconcile-trades"), methods=["POST"]),
        Route(f"{api}/actions/resolve-markets", _action_handler("resolve-markets"), methods=["POST"]),
        Route(f"{api}/actions/update-pnl", _action_handler("update-pnl"), methods=["POST"]),
        Route(f"{api}/actions/review-outcomes", _action_handler("review-outcomes"), methods=["POST"]),
        Route(f"{api}/actions/evaluate-rules", _action_handler("evaluate-rules"), methods=["POST"]),
        Route(f"{api}/actions/generate-report", _action_handler("generate-report"), methods=["POST"]),
        Route(f"{api}/actions/generate-weekly-report", _action_handler("generate-weekly-report"), methods=["POST"]),
        Route(f"{api}/actions/health-check", _action_handler("health-check"), methods=["POST"]),
        Route(f"{api}/actions/reset-portfolio", _action_handler("reset-portfolio"), methods=["POST"]),
        Route(f"{api}/actions/retry-job/{{id}}", retry_job, methods=["POST"]),
        Route(f"{api}/actions/rollback-rule/{{version}}", rollback_rule, methods=["POST"]),
    ]
    routes.extend(ui_routes(conn, config))
    return Starlette(routes=routes, lifespan=lifespan)


async def _portfolio_health(conn) -> dict:
    """Portfolio fields for the health payload (open positions, equity, last snapshot)."""
    from .jobs.portfolio_view import get_active_portfolio_id

    portfolio_id = await get_active_portfolio_id(conn)
    if portfolio_id is None:
        return {"open_positions": 0, "equity_usd": None, "last_pnl_snapshot_at": None}
    cur = await conn.execute(
        "SELECT COUNT(*) FROM paper_trades WHERE paper_portfolio_id = ? AND status = 'open'",
        (portfolio_id,),
    )
    open_positions = int((await cur.fetchone())[0])
    cur = await conn.execute(
        """
        SELECT equity, collected_at FROM pnl_snapshots
         WHERE paper_portfolio_id = ? ORDER BY collected_at DESC LIMIT 1
        """,
        (portfolio_id,),
    )
    row = await cur.fetchone()
    if row is None:
        cur = await conn.execute(
            "SELECT cash_balance FROM paper_portfolios WHERE id = ?", (portfolio_id,)
        )
        cash = int((await cur.fetchone())[0])
        return {"open_positions": open_positions,
                "equity_usd": str(dbmod.micro_to_usd(cash)),
                "last_pnl_snapshot_at": None}
    return {
        "open_positions": open_positions,
        "equity_usd": str(dbmod.micro_to_usd(int(row[0] or 0))),
        "last_pnl_snapshot_at": row[1],
    }
