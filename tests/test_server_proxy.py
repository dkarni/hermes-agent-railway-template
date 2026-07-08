from __future__ import annotations

import base64

import httpx
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.routing import Route
from starlette.testclient import TestClient

import server as hermes_server


def _auth_header(password: str = "pw") -> dict[str, str]:
    token = base64.b64encode(f"admin:{password}".encode("ascii")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def _proxy_app() -> Starlette:
    return Starlette(
        routes=[Route("/polymarket", hermes_server.poly_proxy, methods=["GET"])],
        middleware=[Middleware(AuthenticationMiddleware, backend=hermes_server.BasicAuthBackend())],
    )


def test_polymarket_proxy_retries_worker_startup_gap(monkeypatch):
    class FlakyWorkerClient:
        def __init__(self):
            self.calls = 0

        async def request(self, method, url, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise httpx.ConnectError("", request=httpx.Request(method, url))
            return httpx.Response(200, content=b"worker ok", headers={"content-type": "text/plain"})

    fake = FlakyWorkerClient()
    monkeypatch.setattr(hermes_server, "ADMIN_PASSWORD", "pw")
    monkeypatch.setattr(hermes_server, "poly_http", fake)
    monkeypatch.setattr(hermes_server, "POLY_PROXY_RETRY_DELAYS", (0,))

    with TestClient(_proxy_app()) as client:
        response = client.get("/polymarket", headers=_auth_header())

    assert response.status_code == 200
    assert response.text == "worker ok"
    assert fake.calls == 2


def test_polymarket_proxy_reports_worker_url_after_retries(monkeypatch):
    class DownWorkerClient:
        async def request(self, method, url, **kwargs):
            raise httpx.ConnectError("", request=httpx.Request(method, url))

    monkeypatch.setattr(hermes_server, "ADMIN_PASSWORD", "pw")
    monkeypatch.setattr(hermes_server, "poly_http", DownWorkerClient())
    monkeypatch.setattr(hermes_server, "POLY_PROXY_RETRY_DELAYS", (0,))
    monkeypatch.setattr(hermes_server, "POLY_WORKER_URL", "https://poly.example")

    with TestClient(_proxy_app()) as client:
        response = client.get("/polymarket", headers=_auth_header())

    assert response.status_code == 502
    assert response.json() == {
        "error": "polymarket worker unavailable",
        "detail": "ConnectError",
        "worker_url": "https://poly.example",
    }


def test_polymarket_proxy_injects_bearer_token(monkeypatch):
    """When POLY_API_TOKEN is set, the proxy authenticates to the remote worker."""
    seen = {}

    class CapturingClient:
        async def request(self, method, url, **kwargs):
            seen["headers"] = kwargs.get("headers", {})
            return httpx.Response(200, content=b"ok", headers={"content-type": "text/plain"})

    monkeypatch.setattr(hermes_server, "ADMIN_PASSWORD", "pw")
    monkeypatch.setattr(hermes_server, "poly_http", CapturingClient())
    monkeypatch.setattr(hermes_server, "POLY_API_TOKEN", "secret123")

    with TestClient(_proxy_app()) as client:
        response = client.get("/polymarket", headers=_auth_header())

    assert response.status_code == 200
    assert seen["headers"].get("Authorization") == "Bearer secret123"
