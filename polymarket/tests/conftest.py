from __future__ import annotations

import json
import os

import pytest

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def load_fixture(name: str):
    with open(os.path.join(FIXTURE_DIR, name), "r", encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture
def fixtures():
    return load_fixture
