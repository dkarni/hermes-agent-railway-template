"""Release-blocking safety tests (DESIGN.md sec 4, PRD sec 5 / 26.3)."""

from __future__ import annotations

import glob
import os

import pytest

from ..config import ConfigError, load_config
from ..http import AllowlistClient, DisallowedHostError

POLY_ROOT = os.path.dirname(os.path.dirname(__file__))

FORBIDDEN_STRINGS = [
    "py_clob_client",
    "eth_account",
    "web3",
    "private_key",
    "PRIVATE_KEY",
    "mnemonic",
    "signTypedData",
    "sign_order",
    "create_order",
    "postOrder",
]


def _source_files() -> list[str]:
    files = glob.glob(os.path.join(POLY_ROOT, "**", "*.py"), recursive=True)
    # Exclude the test tree itself (it names the forbidden strings on purpose).
    return [f for f in files if os.path.join("tests", "") not in f]


def test_no_forbidden_strings_in_sources():
    offenders: list[tuple[str, str]] = []
    for path in _source_files():
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
        for needle in FORBIDDEN_STRINGS:
            if needle in text:
                offenders.append((path, needle))
    assert not offenders, f"forbidden trading tokens present: {offenders}"


def test_no_order_placement_paths_in_sources():
    # No signing/order-placement endpoint paths anywhere in the tree.
    forbidden_paths = ["/order", "/orders", "postorder"]
    offenders: list[tuple[str, str]] = []
    for path in _source_files():
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read().lower()
        for needle in forbidden_paths:
            if needle in text:
                offenders.append((path, needle))
    assert not offenders, f"order-placement path referenced: {offenders}"


def test_config_refuses_live_mode():
    with pytest.raises(ConfigError):
        load_config({"TRADING_MODE": "live"})


def test_config_accepts_paper_mode(tmp_path):
    config = load_config({"TRADING_MODE": "paper", "POLY_DATA_DIR": str(tmp_path)})
    assert config.trading_mode == "paper"


@pytest.mark.asyncio
async def test_allowlist_blocks_unknown_host():
    client = AllowlistClient(["gamma-api.polymarket.com"])
    try:
        with pytest.raises(DisallowedHostError):
            await client.get_json("https://example.com/whatever")
    finally:
        await client.aclose()


def test_allowlist_contains_only_expected_hosts(tmp_path):
    config = load_config({"TRADING_MODE": "paper", "POLY_DATA_DIR": str(tmp_path)})
    assert config.allowed_hosts() == frozenset(
        {
            "gamma-api.polymarket.com",
            "data-api.polymarket.com",
            "clob.polymarket.com",
            "api.telegram.org",
        }
    )
