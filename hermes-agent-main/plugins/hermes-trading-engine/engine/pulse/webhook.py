"""Minimal, secure HTTP listener for TradingView indicator alerts (OBSERVE-ONLY).

Stdlib-only (no new deps, no extra uvicorn process). Bound to 127.0.0.1 by DEFAULT so it is
private to the host — expose it deliberately via an SSH tunnel or an authenticated reverse
proxy, never directly. It accepts ONLY ``POST <path>`` with a JSON body and hands the raw bytes
to :class:`engine.pulse.tradingview.TradingViewIntake`, which authenticates (shared secret),
validates, de-duplicates, and records the alert as an observe-only candidate signal.

It has NO trading authority: it cannot place, resize, or bypass a trade — it only records
signals for the existing strategy + execution gate to (independently) consider.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

logger = logging.getLogger("hte.pulse.webhook")

MAX_BODY_BYTES = 64 * 1024          # tiny alerts only; reject anything larger


def _data_dir() -> Path:
    return Path(os.environ.get("HTE_DATA_DIR", "/data"))


def _read_json(name: str) -> "dict | None":
    p = _data_dir() / name
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _make_handler(intake, path: str, header_name: str):
    class _Handler(BaseHTTPRequestHandler):
        server_version = "HermesPulseWebhook/1.0"
        protocol_version = "HTTP/1.1"

        def _send(self, code: int, body: dict) -> None:
            payload = json.dumps(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            try:
                self.wfile.write(payload)
            except Exception:  # noqa: BLE001
                pass

        def _send_html(self, html: str) -> None:
            payload = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            try:
                self.wfile.write(payload)
            except Exception:  # noqa: BLE001
                pass

        def do_GET(self):  # noqa: N802 — read-only views; never accepts signals
            p = self.path.split("?", 1)[0].rstrip("/")
            if p in ("", "/dashboard"):                     # read-only dashboard (port 80)
                try:
                    from engine.pulse.dashboard import DASHBOARD_HTML
                    self._send_html(DASHBOARD_HTML)
                except Exception:  # noqa: BLE001
                    self._send(500, {"ok": False, "reason": "dashboard_unavailable"})
            elif p == "/api/polymarket/training/btc_pulse":
                st = _read_json("btc_pulse_status.json")
                self._send(200, ({"available": True, **st} if st else
                                 {"available": False, "reason": "no status yet"}))
            elif p == "/api/polymarket/training/btc_pulse/ledger":
                led = _read_json("btc_pulse_ledger.json")
                self._send(200, ({"available": True, **led} if led else
                                 {"available": False, "reason": "no ledger yet"}))
            elif p == "/api/health":
                sp = _data_dir() / "btc_pulse_status.json"
                age = (round(time.time() - sp.stat().st_mtime, 1) if sp.exists() else None)
                st = _read_json("btc_pulse_status.json") or {}
                self._send(200, {"status": "ok", "paper_only": True, "live_trading_enabled": False,
                                 "pulse_status_fresh": (age is not None and age < 120),
                                 "pulse_status_age_s": age, "ticks": st.get("ticks")})
            elif p in ("/health", "/healthz"):
                self._send(200, {"status": "ok", "observe_only": True,
                                 "paper_only": True, "live_trading_enabled": False})
            else:
                self._send(404, {"ok": False, "reason": "not_found"})

        def do_POST(self):  # noqa: N802
            if self.path.split("?", 1)[0].rstrip("/") != path.rstrip("/"):
                self._send(404, {"ok": False, "reason": "not_found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0") or "0")
            except ValueError:
                length = 0
            if length <= 0 or length > MAX_BODY_BYTES:
                self._send(413 if length > MAX_BODY_BYTES else 400,
                           {"ok": False, "reason": "bad_content_length", "observe_only": True})
                return
            raw = self.rfile.read(length)
            header_secret = self.headers.get(header_name)
            try:
                code, body = intake.ingest(raw, provided_header=header_secret)
            except Exception as exc:  # noqa: BLE001 — never crash the listener
                logger.exception("tradingview webhook ingest error: %s", exc)
                self._send(500, {"ok": False, "reason": "internal_error", "observe_only": True})
                return
            self._send(code, body)

        def log_message(self, fmt, *args):  # silence default stderr access logging
            logger.debug("webhook %s - %s", self.address_string(), fmt % args)

    return _Handler


class WebhookServer:
    """Owns the background HTTP thread + exposes status for the report."""

    def __init__(self, intake, *, host: str = "127.0.0.1", port: int = 8787,
                 path: str = "/webhooks/tradingview", header_name: str = "X-Tradingview-Secret"):
        self.host = host
        self.port = port
        self.path = path
        self._intake = intake
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._started = False
        self._header_name = header_name

    def start(self) -> "WebhookServer":
        if self._started:
            return self
        handler = _make_handler(self._intake, self.path, self._header_name)
        self._httpd = ThreadingHTTPServer((self.host, self.port), handler)
        self.port = self._httpd.server_address[1]          # resolve ephemeral port (tests)
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        kwargs={"poll_interval": 0.5},
                                        name="tv-webhook", daemon=True)
        self._thread.start()
        self._started = True
        logger.info("TradingView webhook listening host=%s port=%d path=%s observe_only=true",
                    self.host, self.port, self.path)
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:  # noqa: BLE001
                pass
        self._started = False

    def status(self) -> dict:
        return {"listening": self._started, "running": self._started, "observe_only": True,
                "host": self.host, "port": self.port, "path": self.path,
                "bound_internal": self.host in ("127.0.0.1", "localhost")}
