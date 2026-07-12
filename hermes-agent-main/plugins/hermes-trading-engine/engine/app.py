"""Slim read-only API for the BTC 5-minute pulse PAPER engine.

After the focused redesign, the only HTTP surface is health + read-only pulse status/ledger
(served from the JSON the pulse engine writes to ``HTE_DATA_DIR``). There is no trading,
mode, or live-execution endpoint — this engine is PAPER ONLY and the loop runs in the
separate ``scripts/run_btc_pulse.py`` process.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.middleware.gzip import GZipMiddleware

from engine.pulse.dashboard import DASHBOARD_HTML as _DASHBOARD_HTML
from engine.pulse.dashboard_trades import (
    lane_stats,
    lane_trades_for_dashboard,
    recent_trades_for_dashboard,
)

logger = logging.getLogger("hte.app")

app = FastAPI(title="Hermes BTC 5-min Pulse (paper)", version="2.0")
app.add_middleware(GZipMiddleware, minimum_size=1000)


def _data_dir() -> Path:
    return Path(os.environ.get("HTE_DATA_DIR", "/data"))


def _read_json(name: str) -> "dict | None":
    path = _data_dir() / name
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


@app.get("/api/health")
def health() -> dict:
    """Liveness + freshness of the pulse engine (status JSON written every tick)."""
    st = _read_json("btc_pulse_status.json")
    fresh = False
    age = None
    p = _data_dir() / "btc_pulse_status.json"
    if p.exists():
        age = round(time.time() - p.stat().st_mtime, 1)
        fresh = age < 120
    return {"status": "ok", "paper_only": True, "live_trading_enabled": False,
            "pulse_status_fresh": fresh, "pulse_status_age_s": age,
            "ticks": (st or {}).get("ticks")}


@app.get("/api/polymarket/training/btc_pulse")
def btc_pulse_status() -> dict:
    """BTC 5-min pulse engine status: price/vol health, paper ledger, calibration, gating."""
    st = _read_json("btc_pulse_status.json")
    if not st:
        return {"available": False,
                "reason": "pulse engine has not written status yet — start run_btc_pulse.py"}
    return {"available": True, **st}


@app.get("/api/polymarket/training/btc_pulse/ledger")
def btc_pulse_ledger(summary: bool = Query(False)) -> dict:
    """BTC 5-min pulse PAPER ledger: paper positions + realized P&L."""
    led = _read_json("btc_pulse_ledger.json")
    if not led:
        return {"available": False, "reason": "no pulse ledger yet."}
    if summary:
        lane_trades = lane_trades_for_dashboard(led, limit=50)
        return {
            "available": True,
            "paper_only": led.get("paper_only", True),
            "lane_stats": lane_stats(led),
            "lane_trades": lane_trades,
            "positions": recent_trades_for_dashboard(led, limit=50),
        }
    return {"available": True, **led}


@app.get("/api/polymarket/training/btc_pulse/light")
def btc_pulse_light() -> dict:
    """Compact pulse report for quick health checks (written each tick)."""
    rep = _read_json("btc_pulse_light_report.json")
    if not rep:
        return {"available": False, "reason": "no light report yet."}
    return {"available": True, **rep}


@app.get("/api")
def api_index() -> JSONResponse:
    return JSONResponse({"engine": "btc-5min-pulse", "paper_only": True,
                         "endpoints": ["/api/health", "/api/polymarket/training/btc_pulse",
                                       "/api/polymarket/training/btc_pulse/light",
                                       "/api/polymarket/training/btc_pulse/ledger",
                                       _tv_webhook_path()]})


def _tv_webhook_path() -> str:
    return (os.getenv("TRADINGVIEW_WEBHOOK_PATH", "/webhooks/tradingview")
            or "/webhooks/tradingview").strip()


def _tv_webhook_upstream() -> str:
    return (os.getenv("TRADINGVIEW_WEBHOOK_UPSTREAM", "http://hermes-training:8787")
            or "http://hermes-training:8787").rstrip("/")


async def _mirror_tv_webhook(body: bytes, headers: dict[str, str]) -> None:
    """Optional duplicate POST to a paired bot (A/B TV feed without extra TradingView alerts)."""
    mirror = (os.getenv("TRADINGVIEW_WEBHOOK_MIRROR_URL") or "").strip()
    if not mirror:
        return
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            await client.post(mirror, content=body, headers=headers)
    except httpx.HTTPError as exc:
        logger.warning("tradingview webhook mirror failed url=%s err=%s", mirror, exc)


def _dashboard_html() -> str:
    label = (os.getenv("PULSE_DASHBOARD_BOT_LABEL") or "").strip()
    if not label:
        return _DASHBOARD_HTML
    badge = f'<span class="tag live">{label}</span>'
    return _DASHBOARD_HTML.replace(
        '<span class="tag">Paper only</span>',
        f"{badge}<span class=\"tag\">Paper only</span>",
        1,
    )


@app.post(_tv_webhook_path())
async def tradingview_webhook_proxy(request: Request) -> Response:
    """Proxy TradingView alerts to the pulse loop webhook on port 80.

    TradingView only allows HTTP on port 80. The real listener runs inside ``hermes-training``;
    this endpoint forwards POST bodies unchanged (observe-only intake).
    """
    body = await request.body()
    headers: dict[str, str] = {}
    for name in ("Content-Type", "X-Tradingview-Secret"):
        val = request.headers.get(name)
        if val:
            headers[name] = val
    asyncio.create_task(_mirror_tv_webhook(body, dict(headers)))
    url = f"{_tv_webhook_upstream()}{_tv_webhook_path()}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            upstream = await client.post(url, content=body, headers=headers)
    except httpx.ConnectError:
        logger.warning("tradingview webhook upstream unavailable url=%s", url)
        return JSONResponse(
            {"ok": False, "reason": "webhook_upstream_unavailable", "observe_only": True,
             "hint": "set TRADINGVIEW_WEBHOOK_SECRET on hermes-training and redeploy"},
            status_code=503,
        )
    except httpx.HTTPError as exc:
        logger.warning("tradingview webhook proxy error url=%s err=%s", url, exc)
        return JSONResponse(
            {"ok": False, "reason": "webhook_proxy_error", "observe_only": True},
            status_code=502,
        )
    media = upstream.headers.get("content-type", "application/json")
    return Response(content=upstream.content, status_code=upstream.status_code, media_type=media)


@app.get("/", response_class=HTMLResponse)
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard() -> HTMLResponse:
    """Read-only live dashboard for the BTC pulse paper engine (5m + 15m)."""
    return HTMLResponse(_dashboard_html())

