"""Configuration loading and validation (DESIGN.md sec 3).

The only hard requirement is TRADING_MODE == "paper"; everything else has a
default so the worker boots with zero Railway changes. Strategy parameters live
in the seeded rule_sets row, not here.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class ConfigError(SystemExit):
    """Raised (as SystemExit) when configuration is invalid or unsafe."""


def _get(env: dict[str, str], key: str, default: str) -> str:
    value = env.get(key)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _get_int(env: dict[str, str], key: str, default: int, *, minimum: int | None = None) -> int:
    raw = _get(env, key, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"[polymarket] {key} must be an integer, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"[polymarket] {key} must be >= {minimum}, got {value}")
    return value


def _get_decimal(env: dict[str, str], key: str, default: str, *, minimum: Decimal | None = None) -> Decimal:
    raw = _get(env, key, default)
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise ConfigError(f"[polymarket] {key} must be numeric, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"[polymarket] {key} must be >= {minimum}, got {value}")
    return value


def _get_bool(env: dict[str, str], key: str, default: bool) -> bool:
    raw = _get(env, key, "true" if default else "false").lower()
    return raw in ("1", "true", "yes", "on")


def _get_host(url: str) -> str:
    host = urlsplit(url).hostname
    if not host:
        raise ConfigError(f"[polymarket] invalid base URL (no host): {url!r}")
    return host


@dataclass(frozen=True)
class Config:
    trading_mode: str
    data_dir: str
    port: int

    gamma_base_url: str
    data_base_url: str
    clob_base_url: str
    telegram_base_url: str

    leaderboard_wallet_limit: int
    leaderboard_categories: tuple[str, ...]
    wallet_lookback_days: int
    tracked_wallet_limit: int
    tracked_wallet_poll_seconds: int
    market_data_max_age_seconds: int

    paper_starting_bankroll: Decimal
    paper_max_open_positions: int
    paper_max_position_usd: Decimal
    paper_max_wallet_exposure_percent: Decimal
    paper_max_category_exposure_percent: Decimal
    paper_max_event_exposure_percent: Decimal
    paper_max_copies_per_wallet_per_day: int

    rule_update_enabled: bool
    report_timezone: str
    daily_report_time: str

    telegram_chat_id: str  # retained for config compatibility; unused (delivery is dashboard-only)
    demo_mode: bool
    log_level: str

    http_rate_limit_per_second: Decimal

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "polymarket.db")

    @property
    def migrations_dir(self) -> str:
        return os.path.join(os.path.dirname(__file__), "migrations")

    @property
    def report_tz(self) -> ZoneInfo:
        return ZoneInfo(self.report_timezone)

    def allowed_hosts(self) -> frozenset[str]:
        return frozenset(
            {
                _get_host(self.gamma_base_url),
                _get_host(self.data_base_url),
                _get_host(self.clob_base_url),
                _get_host(self.telegram_base_url),
            }
        )


def load_config(env: dict[str, str] | None = None) -> Config:
    env = dict(os.environ if env is None else env)

    trading_mode = _get(env, "TRADING_MODE", "paper")
    if trading_mode != "paper":
        raise ConfigError(
            "[polymarket] refusing to start: TRADING_MODE must be 'paper' in version one "
            f"(got {trading_mode!r}). Live trading is a separate, unapproved project phase."
        )

    daily_report_time = _get(env, "DAILY_REPORT_TIME", "21:00")
    if not _TIME_RE.match(daily_report_time):
        raise ConfigError(
            f"[polymarket] DAILY_REPORT_TIME must be HH:MM 24h, got {daily_report_time!r}"
        )

    report_timezone = _get(env, "REPORT_TIMEZONE", "Europe/Madrid")
    try:
        ZoneInfo(report_timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"[polymarket] REPORT_TIMEZONE is not a valid zone: {report_timezone!r}") from exc

    categories_raw = _get(env, "LEADERBOARD_CATEGORIES", "OVERALL,POLITICS,SPORTS,CRYPTO")
    categories = tuple(c.strip().upper() for c in categories_raw.split(",") if c.strip())
    if not categories:
        raise ConfigError("[polymarket] LEADERBOARD_CATEGORIES must list at least one category")

    config = Config(
        trading_mode=trading_mode,
        data_dir=_get(env, "POLY_DATA_DIR", "/data/polymarket"),
        port=_get_int(env, "POLY_PORT", 8700, minimum=1),
        gamma_base_url=_get(env, "POLYMARKET_GAMMA_BASE_URL", "https://gamma-api.polymarket.com").rstrip("/"),
        data_base_url=_get(env, "POLYMARKET_DATA_BASE_URL", "https://data-api.polymarket.com").rstrip("/"),
        clob_base_url=_get(env, "POLYMARKET_CLOB_BASE_URL", "https://clob.polymarket.com").rstrip("/"),
        telegram_base_url=_get(env, "POLYMARKET_TELEGRAM_BASE_URL", "https://api.telegram.org").rstrip("/"),
        leaderboard_wallet_limit=_get_int(env, "LEADERBOARD_WALLET_LIMIT", 500, minimum=1),
        leaderboard_categories=categories,
        wallet_lookback_days=_get_int(env, "WALLET_LOOKBACK_DAYS", 30, minimum=1),
        tracked_wallet_limit=_get_int(env, "TRACKED_WALLET_LIMIT", 30, minimum=1),
        tracked_wallet_poll_seconds=_get_int(env, "TRACKED_WALLET_POLL_SECONDS", 60, minimum=1),
        market_data_max_age_seconds=_get_int(env, "MARKET_DATA_MAX_AGE_SECONDS", 120, minimum=1),
        paper_starting_bankroll=_get_decimal(env, "PAPER_STARTING_BANKROLL", "1000", minimum=Decimal("0")),
        paper_max_open_positions=_get_int(env, "PAPER_MAX_OPEN_POSITIONS", 25, minimum=1),
        paper_max_position_usd=_get_decimal(env, "PAPER_MAX_POSITION_USD", "20", minimum=Decimal("0")),
        paper_max_wallet_exposure_percent=_get_decimal(
            env, "PAPER_MAX_WALLET_EXPOSURE_PERCENT", "15", minimum=Decimal("0")
        ),
        paper_max_category_exposure_percent=_get_decimal(
            env, "PAPER_MAX_CATEGORY_EXPOSURE_PERCENT", "40", minimum=Decimal("0")
        ),
        paper_max_event_exposure_percent=_get_decimal(
            env, "PAPER_MAX_EVENT_EXPOSURE_PERCENT", "10", minimum=Decimal("0")
        ),
        paper_max_copies_per_wallet_per_day=_get_int(
            env, "PAPER_MAX_COPIES_PER_WALLET_PER_DAY", 3, minimum=0
        ),
        rule_update_enabled=_get_bool(env, "RULE_UPDATE_ENABLED", True),
        report_timezone=report_timezone,
        daily_report_time=daily_report_time,
        telegram_chat_id=_get(env, "POLY_TELEGRAM_CHAT_ID", ""),
        demo_mode=_get_bool(env, "DEMO_MODE", False),
        log_level=_get(env, "LOG_LEVEL", "INFO").upper(),
        http_rate_limit_per_second=_get_decimal(
            env, "POLY_HTTP_RATE_LIMIT_PER_SECOND", "5", minimum=Decimal("0.1")
        ),
    )
    # Force host validation early so a bad base URL fails at load time.
    config.allowed_hosts()
    return config
