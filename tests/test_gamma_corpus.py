"""Task 2 — real-corpus loader machinery (offline tests against fixtures).

No network here: these tests pin the parsing, caching, and decision-point
reconstruction. The actual Gamma pull runs via scripts/pull_gamma_corpus.py
in an environment whose network policy allows Polymarket.

Honesty invariants under test:
  * only crypto up/down markets in the live scope are accepted;
  * resolution outcome comes from the API's resolved outcomePrices, mapped
    through the outcome names (never from a model);
  * NO q fabrication: decision points carry q = p (explicit placeholder,
    flagged in meta) until a real model join fills them;
  * markets without usable price history are dropped AND counted — coverage
    is reported, never silently truncated.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backtest import gamma_corpus as gc

FIXTURES = Path(__file__).parent / "fixtures"


def _rows() -> list[dict]:
    return json.loads((FIXTURES / "gamma_markets_fixture.json").read_text())["rows"]


def _prices() -> dict:
    raw = json.loads((FIXTURES / "clob_prices_fixture.json").read_text())
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def test_parse_accepts_only_scoped_updown_markets():
    parsed = [gc.parse_updown_market(r) for r in _rows()]
    kept = [m for m in parsed if m is not None]
    assert {m.slug for m in kept} == {
        "btc-updown-5m-1784113500",
        "eth-updown-5m-1784113500",
        "btc-updown-15m-1784113200",
        "sol-updown-5m-1784113800",
    }
    by_slug = {m.slug: m for m in kept}
    a = by_slug["btc-updown-5m-1784113500"]
    assert a.asset == "btc" and a.window_sec == 300
    assert a.outcome_up is True  # outcomes [Up, Down], prices [1, 0]
    assert a.clob_token_up == "111111"
    # Outcome order flipped: outcomes [Down, Up], prices [0.9995, 0.0005] → Down won
    b = by_slug["eth-updown-5m-1784113500"]
    assert b.outcome_up is False
    assert b.clob_token_up == "444444"  # Up is the second token here
    # 15m window, prices [0, 1] on [Up, Down] → Down won
    c = by_slug["btc-updown-15m-1784113200"]
    assert c.window_sec == 900 and c.outcome_up is False
    # Unresolved market parses but has no outcome
    d = by_slug["sol-updown-5m-1784113800"]
    assert d.outcome_up is None


def test_reconstruct_decisions_from_price_history():
    by_slug = {
        m.slug: m
        for m in (gc.parse_updown_market(r) for r in _rows())
        if m is not None
    }
    prices = _prices()
    mkt = by_slug["btc-updown-5m-1784113500"]
    hist = gc.parse_price_history(prices[mkt.clob_token_up])
    decisions = gc.reconstruct_decisions(mkt, hist, fracs=(0.3, 0.6, 0.85))
    assert len(decisions) == 3
    # window 11:00:00Z → 11:05:00Z; frac 0.6 → t_d = start + 180s = ...13380
    d60 = next(d for d in decisions if abs(d.lifetime_frac - 0.6) < 1e-9)
    assert d60.p == pytest.approx(0.72)  # last history point at/before t_d
    assert d60.days_to_resolution == pytest.approx(120.0 / 86400.0)
    # Honesty: no fabricated model q — placeholder equals p and is flagged
    assert d60.q == d60.p
    assert d60.meta["q_source"] == "market_placeholder_no_model"
    assert d60.meta["source"] == "gamma_corpus"
    assert d60.resolved_yes is True
    # true_q is the realized outcome (diagnostic only)
    assert d60.true_q == 1.0
    # Chronology
    ts = [d.decision_time for d in decisions]
    assert ts == sorted(ts)


def test_reconstruct_skips_stale_or_missing_prices():
    by_slug = {
        m.slug: m
        for m in (gc.parse_updown_market(r) for r in _rows())
        if m is not None
    }
    prices = _prices()
    # eth market history is sparse: only 3 points; frac 0.3 (t=...13290) has a
    # point 60s earlier (ok), but with a tight staleness budget it must drop.
    mkt = by_slug["eth-updown-5m-1784113500"]
    hist = gc.parse_price_history(prices["444444"])
    tight = gc.reconstruct_decisions(mkt, hist, fracs=(0.3, 0.6, 0.85), max_stale_sec=10)
    assert tight == []
    loose = gc.reconstruct_decisions(mkt, hist, fracs=(0.3, 0.6, 0.85), max_stale_sec=120)
    assert len(loose) >= 2
    # UP-token price is used even when Up is the second outcome
    assert all(0.0 < d.p < 1.0 for d in loose)


def test_load_corpus_from_cache_counts_coverage(tmp_path):
    # Build a fake cache layout: one page + price files
    cache = tmp_path / "gamma"
    (cache / "pages").mkdir(parents=True)
    (cache / "prices").mkdir(parents=True)
    (cache / "pages" / "markets_page_0000.json").write_text(json.dumps(_rows()))
    for token, payload in _prices().items():
        (cache / "prices" / f"{token}.json").write_text(json.dumps(payload))

    corpus = gc.load_corpus(cache_dir=cache, fracs=(0.3, 0.6, 0.85))
    s = corpus.summary
    # 5 fixture rows: 1 out-of-scope, 1 unresolved → 3 resolved in-scope
    assert s.n_rows_seen == 5
    assert s.n_in_scope == 4
    assert s.n_resolved == 3
    # btc-5m has full history; eth-5m sparse but usable; btc-15m has no price file
    assert s.n_with_prices == 2
    assert s.n_decisions == len(corpus.decisions) > 0
    assert all(d.meta["source"] == "gamma_corpus" for d in corpus.decisions)
    # Engine provenance: these must NOT be labeled synthetic
    assert all(not d.meta.get("synthetic") for d in corpus.decisions)


def test_sample_report_renders(tmp_path):
    cache = tmp_path / "gamma"
    (cache / "pages").mkdir(parents=True)
    (cache / "prices").mkdir(parents=True)
    (cache / "pages" / "markets_page_0000.json").write_text(json.dumps(_rows()))
    for token, payload in _prices().items():
        (cache / "prices" / f"{token}.json").write_text(json.dumps(payload))
    text = gc.sample_report(cache_dir=cache, n=20)
    assert "btc-updown-5m-1784113500" in text
    assert "coverage" in text.lower()


def test_pull_path_needs_no_heavy_deps():
    """The VPS pull box has only stdlib+httpx. Importing backtest.gamma_corpus
    and parsing markets must work with numpy/scipy/pydantic/yaml missing —
    a regression here breaks scripts/pull_gamma_corpus.py in the field."""
    import subprocess
    import sys

    blocker = (
        "import sys, json\n"
        "class _Block:\n"
        "    BLOCKED = {'numpy', 'scipy', 'pydantic', 'yaml'}\n"
        "    def find_module(self, name, path=None):\n"
        "        if name.split('.')[0] in self.BLOCKED: return self\n"
        "    def load_module(self, name):\n"
        "        raise ImportError(f'{name} blocked for light-import test')\n"
        "sys.meta_path.insert(0, _Block())\n"
        "from backtest.gamma_corpus import parse_updown_market, parse_price_history\n"
        "row = json.load(open('tests/fixtures/gamma_markets_fixture.json'))['rows'][0]\n"
        "m = parse_updown_market(row)\n"
        "assert m is not None and m.asset == 'btc' and m.outcome_up is True\n"
        "print('LIGHT_IMPORT_OK')\n"
    )
    r = subprocess.run(
        [sys.executable, "-c", blocker],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).parent.parent),
        env={"PYTHONPATH": str(Path(__file__).parent.parent), "PATH": "/usr/bin:/bin"},
    )
    assert "LIGHT_IMPORT_OK" in r.stdout, f"stdout={r.stdout}\nstderr={r.stderr}"


def test_slug_fallback_regex_matches_live_scope():
    """gamma_corpus keeps a stdlib-only copy of the live slug regex for the
    pull path; it must stay identical to hermes.market_scope.SLUG_RE."""
    import re

    from hermes.market_scope import SLUG_RE as live

    src = (Path(__file__).parent.parent / "backtest" / "gamma_corpus.py").read_text()
    m = re.search(r'SLUG_RE = re\.compile\(r"([^"]+)"\)', src)
    assert m, "fallback regex not found in gamma_corpus.py"
    assert m.group(1) == live.pattern


def test_no_circular_example_csv_writer_left():
    """The old demo-CSV writer fabricated q = true_q + noise — the exact
    circular pattern Task 1 removed. It must stay dead."""
    import backtest.historical as hist

    assert not hasattr(hist, "write_example_historical_csv")
