"""Data API adapter: leaderboard and public wallet trades (DESIGN.md sec 1.5).

Endpoints (verified live 2026-07-06):
  GET /v1/leaderboard?timePeriod=MONTH&orderBy=PNL&limit=50&offset=N[&category=X]
  GET /trades?user=<proxyWallet>&limit=N&offset=N
"""

from __future__ import annotations

from typing import Awaitable, Callable

from ..http import AllowlistClient
from .models import LeaderboardEntry, WalletTrade, to_decimal

LEADERBOARD_MAX_LIMIT = 50


def _parse_leaderboard_row(
    row: dict, *, category: str, time_period: str, order_by: str
) -> LeaderboardEntry:
    rank_raw = row.get("rank")
    rank = int(rank_raw) if rank_raw not in (None, "") else None
    return LeaderboardEntry(
        wallet_address=str(row.get("proxyWallet", "")),
        rank=rank,
        pnl=to_decimal(row.get("pnl", 0)),
        volume=to_decimal(row.get("vol", 0)),
        user_name=str(row.get("userName") or ""),
        x_username=str(row.get("xUsername") or ""),
        verified_badge=bool(row.get("verifiedBadge", False)),
        profile_image=str(row.get("profileImage") or ""),
        category=category,
        time_period=time_period,
        order_by=order_by,
        raw=row,
    )


def _parse_trade_row(row: dict) -> WalletTrade:
    idx_raw = row.get("outcomeIndex")
    outcome_index = int(idx_raw) if idx_raw not in (None, "") else None
    return WalletTrade(
        wallet_address=str(row.get("proxyWallet", "")),
        side=str(row.get("side", "")).upper(),
        asset_id=str(row.get("asset", "")),
        condition_id=str(row.get("conditionId", "")),
        size=to_decimal(row.get("size", 0)),
        price=to_decimal(row.get("price", 0)),
        timestamp=int(row.get("timestamp", 0)),
        title=str(row.get("title") or ""),
        slug=str(row.get("slug") or ""),
        event_slug=str(row.get("eventSlug") or ""),
        outcome=str(row.get("outcome") or ""),
        outcome_index=outcome_index,
        transaction_hash=str(row.get("transactionHash") or ""),
        raw=row,
    )


class DataApiAdapter:
    def __init__(self, client: AllowlistClient, base_url: str) -> None:
        self._client = client
        self._base = base_url.rstrip("/")

    async def get_leaderboard(
        self,
        *,
        time_period: str = "MONTH",
        order_by: str = "PNL",
        category: str | None = None,
        limit: int = LEADERBOARD_MAX_LIMIT,
        offset: int = 0,
    ) -> list[LeaderboardEntry]:
        limit = min(limit, LEADERBOARD_MAX_LIMIT)
        params: dict[str, object] = {
            "timePeriod": time_period,
            "orderBy": order_by,
            "limit": limit,
            "offset": offset,
        }
        if category and category.upper() != "OVERALL":
            params["category"] = category.upper()
        data = await self._client.get_json(f"{self._base}/v1/leaderboard", params=params)
        rows = data if isinstance(data, list) else data.get("data", [])
        label = category.upper() if category else "OVERALL"
        return [
            _parse_leaderboard_row(row, category=label, time_period=time_period, order_by=order_by)
            for row in rows
        ]

    async def get_user_trades(
        self, wallet: str, *, limit: int = 100, offset: int = 0
    ) -> list[WalletTrade]:
        params = {"user": wallet, "limit": limit, "offset": offset}
        data = await self._client.get_json(f"{self._base}/trades", params=params)
        rows = data if isinstance(data, list) else data.get("data", [])
        return [_parse_trade_row(row) for row in rows]

    async def iter_user_trades(
        self,
        wallet: str,
        *,
        stop_predicate: Callable[[WalletTrade], bool] | None = None,
        page_size: int = 100,
        max_pages: int = 100,
    ) -> list[WalletTrade]:
        """Walk trade pages until the endpoint is exhausted, stop_predicate is
        satisfied by a trade, or max_pages is reached. Deduplicates by
        (transaction_hash, asset_id, side, timestamp) to protect against the
        overlap window a caller re-queries. Returns trades collected up to (and
        including) the first trade that satisfies the predicate on each page but
        stops fetching further pages once the predicate fires.

        stop_predicate is typically `lambda t: t.timestamp < lookback_cutoff`.
        """
        collected: list[WalletTrade] = []
        seen: set[tuple[str, str, str, int]] = set()
        offset = 0
        for _ in range(max_pages):
            page = await self.get_user_trades(wallet, limit=page_size, offset=offset)
            if not page:
                break
            stop = False
            for trade in page:
                key = (trade.transaction_hash, trade.asset_id, trade.side, trade.timestamp)
                if stop_predicate is not None and stop_predicate(trade):
                    stop = True
                    break
                if key in seen:
                    continue
                seen.add(key)
                collected.append(trade)
            if stop or len(page) < page_size:
                break
            offset += page_size
        return collected
