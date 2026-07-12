"""Port-80 TradingView webhook proxy on the read-only API."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

from engine.app import app

SECRET = "test-secret"


def test_tradingview_proxy_forwards_to_upstream():
    captured: dict = {}

    async def _fake_post(url, *, content, headers):
        captured["url"] = url
        captured["content"] = content
        captured["headers"] = headers
        req = httpx.Request("POST", url)
        return httpx.Response(200, json={"ok": True, "accepted": True, "observe_only": True},
                              request=req)

    mock_client = AsyncMock()
    mock_client.post = _fake_post
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    payload = json.dumps({"secret": SECRET, "bot_name": "hermes", "symbol": "BTCUSD",
                          "direction": "DOWN", "event_id": "proxy-1"}).encode()
    with patch("engine.app.httpx.AsyncClient", return_value=mock_client):
        client = TestClient(app)
        r = client.post("/webhooks/tradingview", content=payload,
                        headers={"Content-Type": "application/json",
                                 "X-Tradingview-Secret": SECRET})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert captured["url"].endswith("/webhooks/tradingview")
    assert captured["content"] == payload
    assert captured["headers"]["X-Tradingview-Secret"] == SECRET


def test_tradingview_proxy_upstream_down_returns_503():
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("engine.app.httpx.AsyncClient", return_value=mock_client):
        client = TestClient(app)
        r = client.post("/webhooks/tradingview", content=b"{}",
                        headers={"Content-Type": "application/json"})
    assert r.status_code == 503
    assert r.json()["reason"] == "webhook_upstream_unavailable"