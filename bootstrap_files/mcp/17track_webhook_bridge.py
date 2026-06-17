#!/usr/bin/env python3
"""Public 17TRACK webhook bridge for Marco.

Receives 17TRACK callbacks on /17track/<token>, signs the body with the
Hermes webhook HMAC secret, and forwards the event to the internal Hermes
webhook endpoint.
"""

import hashlib
import hmac
import json
import os
import uuid
from pathlib import Path

from aiohttp import ClientSession, web


PROFILE_ROOT = Path(__file__).resolve().parents[1]
SUBS_PATH = PROFILE_ROOT / "webhook_subscriptions.json"
ENV_PATHS = (PROFILE_ROOT / ".env", PROFILE_ROOT / "hermes.env")

FORWARD_URL = os.getenv("MARCO_17TRACK_FORWARD_URL", "http://127.0.0.1:8644/webhooks/17track")
HOST = os.getenv("MARCO_17TRACK_BRIDGE_HOST", "0.0.0.0")
PORT = int(os.getenv("MARCO_17TRACK_BRIDGE_PORT", "8654"))
MAX_BODY_BYTES = 1024 * 1024


def _load_env() -> None:
    for env_path in ENV_PATHS:
        if not env_path.exists():
            continue
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _token() -> str:
    token = os.getenv("SEVENTEENTRACK_WEBHOOK_TOKEN")
    if not token:
        raise RuntimeError("SEVENTEENTRACK_WEBHOOK_TOKEN is not configured")
    return token


def _hermes_secret() -> str:
    data = json.loads(SUBS_PATH.read_text(encoding="utf-8"))
    secret = data.get("17track", {}).get("secret")
    if not secret:
        raise RuntimeError(f"Missing 17track secret in {SUBS_PATH}")
    return secret


async def health(_request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "forward_url": FORWARD_URL})


async def receive(request: web.Request) -> web.Response:
    if request.match_info["token"] != _token():
        return web.json_response({"error": "not found"}, status=404)

    body = await request.read()
    if len(body) > MAX_BODY_BYTES:
        return web.json_response({"error": "payload too large"}, status=413)

    signature = hmac.new(_hermes_secret().encode("utf-8"), body, hashlib.sha256).hexdigest()
    request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
    headers = {
        "Content-Type": request.headers.get("Content-Type", "application/json"),
        "X-Webhook-Signature": signature,
        "X-Request-ID": request_id,
    }

    async with ClientSession() as session:
        async with session.post(FORWARD_URL, data=body, headers=headers) as response:
            text = await response.text()
            if response.status >= 400:
                return web.json_response(
                    {"error": "forward failed", "status": response.status, "body": text[:500]},
                    status=502,
                )

    return web.json_response({"accepted": True, "request_id": request_id}, status=202)


def make_app() -> web.Application:
    _load_env()
    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_post("/17track/{token}", receive)
    return app


if __name__ == "__main__":
    web.run_app(make_app(), host=HOST, port=PORT)
