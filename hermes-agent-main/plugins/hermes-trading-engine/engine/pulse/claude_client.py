"""Anthropic Claude client for the BTC pulse loop (OBSERVE/ADVISORY, fail-open, PAPER ONLY).

Used to give the system a SECOND, independent model (different architecture than Grok) for the
maker-checker verifier and the research meta-loop — exactly the "different model catches different
errors" point from loop-engineering. Pure HTTP via the Anthropic Messages API; never raises into
the engine (returns None on any error so the bot keeps running as before)."""

from __future__ import annotations

import json
import os
from typing import Optional

_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"


def anthropic_key() -> str:
    return (os.getenv("ANTHROPIC_API_KEY") or os.getenv("CLAUDE_API_KEY") or "").strip()


def claude_model() -> str:
    return (os.getenv("CLAUDE_MODEL") or "claude-sonnet-4-5").strip()


def claude_chat(prompt: str, *, model: Optional[str] = None, timeout_s: float = 20.0,
                box: dict, max_tokens: int = 1024, system: Optional[str] = None) -> Optional[str]:
    """One Claude Messages call. Returns the text content or None (fail-open). ``box`` caches the
    httpx client across calls."""
    key = anthropic_key()
    if not key:
        return None
    try:
        import httpx
        c = box.get("c")
        if c is None:
            c = httpx.Client(timeout=timeout_s)
            box["c"] = c
        body = {"model": (model or claude_model()), "max_tokens": int(max_tokens),
                "messages": [{"role": "user", "content": prompt}]}
        if system:
            body["system"] = system
        r = c.post(_ANTHROPIC_URL, headers={"x-api-key": key,
                                            "anthropic-version": "2023-06-01",
                                            "content-type": "application/json"}, json=body)
        if r.status_code != 200:
            return None
        parts = (r.json() or {}).get("content") or []
        for p in parts:
            if p.get("type") == "text" and p.get("text"):
                return p["text"]
        return None
    except Exception:  # noqa: BLE001 — never raise into the engine
        return None
