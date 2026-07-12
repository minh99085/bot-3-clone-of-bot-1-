"""Chainlink Data Streams BTC/USD price feed — the EXACT source this market resolves on.

This is the fastest + correct feed (sub-second signed reports), and using it eliminates the
Coinbase-vs-Chainlink basis entirely (we'd read the same feed the market settles on). It is
CREDENTIALED: you must create an API key (UUID) + HMAC secret at the Chainlink Data Streams
self-service portal (https://docs.chain.link/data-streams/sign-up) and provide them, plus the
BTC/USD feed ID, via env / Cloud Agent secrets:

    CHAINLINK_DATA_STREAMS_API_KEY   (UUID)
    CHAINLINK_DATA_STREAMS_SECRET    (HMAC secret)
    CHAINLINK_BTC_FEED_ID            (0x… feed id for BTC/USD)

Read-only: it only GETs the latest signed report and decodes the benchmark price. It never
trades. Fail-open: any missing cred / error returns None so the engine falls back to its
proxy feed. Auth per Chainlink docs: string-to-sign = "METHOD FULL_PATH BODY_HASH API_KEY
TIMESTAMP" (single spaces), signature = hex HMAC-SHA256(secret, string).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from typing import Optional

logger = logging.getLogger("hte.pulse.chainlink_streams")

DEFAULT_REST = "https://api.dataengine.chain.link"
_LATEST_PATH = "/api/v1/reports/latest"
_PRICE_SCALE = 1e18              # Data Streams crypto reports carry an 18-decimal price


def _body_hash(body: bytes = b"") -> str:
    return hashlib.sha256(body).hexdigest()


def sign_headers(api_key: str, secret: str, method: str, full_path: str,
                 *, body: bytes = b"", ts_ms: Optional[int] = None) -> dict:
    """Build the three Chainlink Data Streams auth headers for a request."""
    ts = int(ts_ms if ts_ms is not None else time.time() * 1000)
    string_to_sign = f"{method.upper()} {full_path} {_body_hash(body)} {api_key} {ts}"
    sig = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"),
                   hashlib.sha256).hexdigest()
    return {"Authorization": api_key, "X-Authorization-Timestamp": str(ts),
            "X-Authorization-Signature-SHA256": sig}


def decode_v3_price(full_report_hex: str) -> Optional[float]:
    """Decode the benchmark price from a Data Streams v3 ``fullReport`` hex blob.

    fullReport ABI = (bytes32[3] reportContext, bytes reportData, bytes32[] rs, bytes32[] ss,
    bytes32 rawVs). reportData (v3 crypto) = (bytes32 feedId, uint32 validFromTimestamp,
    uint32 observationsTimestamp, uint192 nativeFee, uint192 linkFee, uint32 expiresAt,
    int192 price, int192 bid, int192 ask) — price is word index 6, signed, /1e18."""
    try:
        h = full_report_hex[2:] if full_report_hex.startswith("0x") else full_report_hex
        raw = bytes.fromhex(h)
        # word 3 (bytes 96:128) is the offset to reportData within the tuple
        rd_off = int.from_bytes(raw[96:128], "big")
        length = int.from_bytes(raw[rd_off:rd_off + 32], "big")
        report_data = raw[rd_off + 32: rd_off + 32 + length]
        # price is word index 6 in reportData
        word = report_data[6 * 32:7 * 32]
        if len(word) < 32:
            return None
        price = int.from_bytes(word, "big", signed=True) / _PRICE_SCALE
        return price if price > 0 else None
    except Exception:  # noqa: BLE001
        return None


def available() -> bool:
    return bool((os.getenv("CHAINLINK_DATA_STREAMS_API_KEY") or "").strip()
                and (os.getenv("CHAINLINK_DATA_STREAMS_SECRET") or "").strip()
                and (os.getenv("CHAINLINK_BTC_FEED_ID") or "").strip())


def chainlink_streams_fetcher(*, api_key: Optional[str] = None, secret: Optional[str] = None,
                              feed_id: Optional[str] = None, rest: Optional[str] = None,
                              timeout_s: float = 4.0):
    """Build a READ-ONLY fetcher ``() -> float | None`` for the BTC/USD benchmark price from
    Chainlink Data Streams. Returns None on any missing cred / error (fail-open)."""
    api_key = api_key or os.getenv("CHAINLINK_DATA_STREAMS_API_KEY", "").strip()
    secret = secret or os.getenv("CHAINLINK_DATA_STREAMS_SECRET", "").strip()
    feed_id = feed_id or os.getenv("CHAINLINK_BTC_FEED_ID", "").strip()
    rest = (rest or os.getenv("CHAINLINK_DATA_STREAMS_REST", DEFAULT_REST)).rstrip("/")
    box: dict = {}

    def _fetch() -> Optional[float]:
        if not (api_key and secret and feed_id):
            return None
        full_path = f"{_LATEST_PATH}?feedID={feed_id}"
        try:
            import httpx
            c = box.get("c")
            if c is None:
                c = httpx.Client(timeout=timeout_s)
                box["c"] = c
            headers = sign_headers(api_key, secret, "GET", full_path)
            r = c.get(rest + full_path, headers=headers)
            if r.status_code != 200:
                logger.debug("data streams %s: %s", r.status_code, r.text[:120])
                return None
            rep = (r.json() or {}).get("report") or {}
            # prefer a decoded field if the API provides one; else decode the report blob
            for k in ("benchmarkPrice", "price"):
                v = rep.get(k)
                if v is not None:
                    try:
                        p = float(v)
                        p = p / _PRICE_SCALE if p > 1e9 else p   # raw int192 vs decimal
                        if p > 0:
                            return p
                    except (TypeError, ValueError):
                        pass
            blob = rep.get("fullReport")
            return decode_v3_price(blob) if blob else None
        except Exception:  # noqa: BLE001 — a price read never raises into the loop
            return None
    return _fetch
