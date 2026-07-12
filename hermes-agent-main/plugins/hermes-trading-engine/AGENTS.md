# Hermes Trading Engine — agent guide

This plugin is now a **focused BTC 5-minute "Up or Down" pulse PAPER engine**. It trades ONLY
the Polymarket `btc-up-or-down-5m` series, in paper mode.

## Operating directive (ALWAYS follow)

You operate as a **Quant Researcher + Developer + Trader** team. Mission: make the BTC 5-min
pulse paper engine profitable, fast.

- **MEMORY — REPO SCOPE (operator set 2026-06-25):** Work **only** in
  `https://github.com/minh99085/Grok-Bot-2`. All commits, pushes, report refreshes, and VPS syncs
  target that repo's `main` branch. Do **not** use `hermes-agent-cursor` unless the operator
  explicitly overrides in the current message.
- **MEMORY — ALWAYS end every response with the exact line `I AM DONE THINKING`** as the final
  line, so the operator knows the answer is complete. This applies to every turn, no exceptions
  (operator reaffirmed 2026-06-24).
- **MEMORY — PROFIT STRATEGY (operator granted full authority 2026-06-24 to make this a profit /
  alpha machine).** The ONLY proven positive edge is the **risk-free within-window arbitrage**
  (`arbitrage.py` dutch book `up_vwap+down_vwap<1`) — MAXIMIZE it (small epsilon above real
  fees/slippage, big depth-capped size). The **directional model is structurally negative-EV**
  (price ≈ probability) — keep it SELECTIVE: directional allowlist ON (trade only Wilson-proven
  winning buckets) + a small exploration carve-out so it never freezes and keeps learning. Keep
  **Grok/Claude observe-only (`shadow`)** — they are not a proven edge; never let an LLM opinion
  drive trades. Never loosen the execution-quality gate. PAPER ONLY, always.
- **MEMORY — ALWAYS REMOVE ORPHANS THEN REBUILD ON EVERY CODEBASE UPDATE.** Every single time you
  change code and deploy, you MUST run `docker compose down --remove-orphans` first, THEN
  `docker compose build` (no service arg → both images), THEN `docker compose up -d --remove-orphans`.
  Never hot-swap a file or recreate a single service in isolation. This is non-negotiable.
- **ALWAYS push every change to BOTH the GitHub `main` repo AND the live VPS**, and keep them in
  sync (ideally SHA-for-SHA) on every turn. Never leave `main` and the VPS diverged. After a code
  change, the standard deploy is: push to `main` → sync the VPS → `docker compose down
  --remove-orphans` → `docker compose build` → `docker compose up -d --remove-orphans` in the pulse
  plugin compose dir, then verify health/reconciliation. **CRITICAL: the trading/persist LOOP runs in the
  `hermes-training` container (`scripts/run_btc_pulse.py`); `hermes-trading-engine` is only the
  API/dashboard. Rebuild + recreate BOTH services (`docker compose build` with NO service arg, then
  `up -d`). Rebuilding only `hermes-trading-engine` leaves the loop on stale code and `/data` keeps
  the OLD report schema — verify the new code is live in `hermes-training`, not just the API.**
- **MEMORY — ALWAYS MERGE AND SYNC IN BOTH THE REPO AND THE VPS.** On every change, MERGE the work
  into `main` (do not leave it stranded on a feature branch) AND deploy/sync the SAME code to the
  live VPS, so the GitHub `main` repo and the VPS run identical code every turn. After deploying,
  reconcile the VPS to a clean `git` state at that commit and verify `git rev-parse HEAD` on the VPS
  equals `origin/main`. Never leave `main` and the VPS diverged, and never leave a change merged in
  one place but not the other — merge + sync both, every time.
- **ALWAYS generate a real full report on VPS and publish only to `vps_full_reports/latest/` on
  `main`.** See repo `.grok/rules/vps-full-report.md`. Engine `_persist` must write `FULL_REPORT.md`
  plus the full provenance bundle to `/data` every tick. On pull: wipe `latest/`, pull fresh from
  VPS, remove stale tracked files, commit + push to `origin/main`. Canonical URL:
  https://github.com/minh99085/Bot-1/tree/main/vps_full_reports/latest

- **HARD SAFETY INVARIANT (never relaxed):** PAPER ONLY. No real order, no wallet, no signing.
  There is no live-execution code path in `engine/pulse`, and `scripts/run_btc_pulse.py`
  refuses to start if any live flag is set. Never add one unless the user explicitly asks.
- **Quality gates** (edge size, depth, etc.) may be loosened per the operator's request — they
  only affect which *paper* trades are taken.
- **External signals are OBSERVE-ONLY.** The TradingView webhook intake (`engine/pulse/tradingview.py`
  + `webhook.py`, bound to `127.0.0.1:8787` by default, enabled only when
  `TRADINGVIEW_WEBHOOK_SECRET` is set) feeds candidate signals ONLY. A TradingView alert may NEVER
  place/resize a trade, bypass the strategy or execution-quality gate, or override the Polymarket
  orderbook checks — it is attached to candidates as `dr.external` and recorded in the report;
  the strategy + execution gate remain the sole trade authority. Never wire it into
  `decide()`/`evaluate_execution()`.
- Don't reintroduce the retired legacy engine (universe scanner, Bregman, Grok advisory,
  micro-live/guarded-live/production-review). It was deliberately removed.

## How it works

The contract resolves `Up` iff `Chainlink_BTC_close >= Chainlink_BTC_open` over the 5-min
window (ties → Up). **Reference model (correct):** the oracle is the **Chainlink Data Streams
reference price** for `btc/usd`, obtained from **Polymarket RTDS** (`crypto_prices_chainlink`,
`engine/pulse/rtds.py`) — the exact feed Polymarket resolves on. Binance/Coinbase are FAST
LEAD predictors only (`engine/pulse/oracle.py` `LeadFeeds`), never settlement truth. The
engine:
1. ingests the rolling windows from Gamma (`engine/pulse/markets.py`);
2. snapshots each window's OPEN + CLOSE price on the RTDS Chainlink oracle (`source=rtds_chainlink`);
3. prices each open window as a digital option
   `P(up)=Phi((ln(S_now/S_open)+(mu-0.5 sig^2) r)/(sig*sqrt(r)))` (`fair_value.py`);
4. takes a loosened paper trade on the higher after-cost-edge side (`strategy.py`,
   `executor.py` — simulated fills only);
5. settles by priority — official **Polymarket resolution** first, then the **RTDS Chainlink
   open/close proxy** only when the close-snapshot lag is within threshold — scores Brier
   calibration + proxy/official reconciliation (`settlement.py`). Classic Chainlink Data Feed /
   AggregatorV3 is rejected as a primary settlement feed (`oracle.py`).

The fast loop + entrypoint are `engine/pulse/engine.py` + `scripts/run_btc_pulse.py`.

## Deployment & sync directive (ALWAYS follow)

**ALWAYS push every completed change to BOTH the GitHub `main` repo AND the live VPS, and
keep them identical (SHA-for-SHA: `origin/main` == VPS `git rev-parse HEAD`).** Never advance
one without the other; verify the SHAs match before calling a task done.

VPS deploy procedure (the VPS cannot `git fetch origin` — use a git bundle or the sync script):

**One command (Windows, from repo root):**
```powershell
git push origin main
.\scripts\sync-vps.ps1              # ALWAYS: sync + down --remove-orphans -> build -> up -d (operator memory)
.\scripts\sync-vps.ps1 -SkipRebuild # code sync only — operator must explicitly request in current message
.\scripts\verify-sync.ps1           # check only; exit 1 if diverged
```

**Manual bundle (if script unavailable):**
1. `git push origin main` — GitHub must lead or match local `main`.
2. `git bundle create /tmp/u.bundle <vps_head_sha>..origin/main`, then `scp` to VPS.
3. On VPS: `git -C /opt/Bot-1 fetch /tmp/u.bundle HEAD:refs/remotes/bundle/main`
   then `git -C /opt/Bot-1 reset --hard bundle/main`.
4. If plugin code changed: in `/opt/Bot-1/hermes-agent-main/plugins/hermes-trading-engine`
   run `docker compose down --remove-orphans` → `docker compose build` →
   `docker compose up -d --remove-orphans`.
5. Verify: `git -C /opt/Bot-1 rev-parse HEAD` == `git rev-parse origin/main` on your
   machine; both containers `healthy`; `/data/btc_pulse_status.json` fresh (<120s).
   Clean up `/tmp/*.bundle`.

### VPS access (Bot-1 — **canonical**)
- Host `144.202.122.120`, user `root`, port `22`.
- Connect: `ssh root@144.202.122.120` (or `ssh -i <key> root@144.202.122.120`).
- Dashboard: http://144.202.122.120/
- **Retired:** `45.32.227.242` / `linuxuser`, `45.32.224.147` — do not use.
- Repo root: `/opt/Bot-1` (synced from `Bot-1` via git bundle; VPS cannot `git fetch`).
- Plugin path: `/opt/Bot-1/hermes-agent-main/plugins/hermes-trading-engine`.
- Containers: `hermes-training` (pulse loop) + `hermes-trading-engine` (API).
- Windows operator key (if configured): `%USERPROFILE%\.ssh\bot1_grok_temp`.
- The VPS cannot `git fetch origin` — sync via git bundle (procedure above).

## Run it

From `plugins/hermes-trading-engine`:

```bash
docker compose up -d --build      # build + start the pulse loop + API
docker compose logs -f hermes-training        # watch the pulse loop
# Dashboard (browser): http://<vps-ip>/dashboard when PULSE_DASHBOARD_PUBLISH=0.0.0.0:80
# status:  curl http://localhost:8800/api/polymarket/training/btc_pulse
# ledger:  curl http://localhost:8800/api/polymarket/training/btc_pulse/ledger
```

`PULSE_*` env (see `docker-compose.yml`) tunes the loosened gates (tick cadence, size,
min-edge, depth, price cap). Smoke test without Docker: `python scripts/run_btc_pulse.py
--max-ticks 3`.

## Tests

`python -m pytest tests/` — `tests/test_btc_pulse_engine.py` covers ingestion, the digital
fair value, rolling vol, open-snapshot gating, the loosened decision, paper fill + settlement
P&L, calibration, and a full deterministic trade→settle→calibrate cycle.

## Cursor Cloud specific instructions

These notes are for running this plugin **locally (venv) in a Cloud Agent VM**, where Docker is
not pre-installed. The venv lives at `plugins/hermes-trading-engine/.venv` (gitignored). Run all
commands from `plugins/hermes-trading-engine` with the venv activated (`. .venv/bin/activate`).

- **Python 3.12 is fine** here even though the Dockerfile pins 3.11 — all deps
  (`fastapi`/`uvicorn`/`httpx`/`websockets`/`ortools`/`python-docx`) have 3.12 wheels.
- **Run the engine (smoke / dev loop):** set a writable data dir first, since the default `/data`
  is not writable outside Docker:
  `HTE_DATA_DIR=/tmp/hte-data python scripts/run_btc_pulse.py --max-ticks 3`. This makes **live**
  calls to Polymarket Gamma + CLOB and a CEX price feed. **Binance returns HTTP 451 (geo-block)
  from this VM** — that is expected and harmless; the engine falls back to Coinbase.
- **Run the API/dashboard:** `HTE_DATA_DIR=/tmp/hte-data uvicorn engine.app:app --host 127.0.0.1
  --port 8800`, then `GET /api/health`, `/api/polymarket/training/btc_pulse`, and `/dashboard`.
  The API is **read-only and serves whatever the loop wrote to `HTE_DATA_DIR`** — point it at the
  same dir the engine ran against, or every endpoint reports `available: false`.
- **Tests are slow + partly live.** The full suite is ~612 tests and several drive the engine
  through many ticks (with real network calls / sleeps), so a serial `python -m pytest tests/`
  takes ~10+ min and can appear to "hang" around 40% on the slow ticking tests (it is not stuck).
  Prefer parallel + a generous per-test cap: `pip install pytest-xdist pytest-timeout` (both are
  dev-only, not in `requirements*.txt`), then
  `python -m pytest tests/ -n 4 --timeout=300 --timeout-method=thread`. Use **`--timeout-method=thread`**,
  not `signal`: a couple of tests block in C extensions where SIGALRM cannot fire.
  `test_pulse_selectivity.py::test_engine_underdog_floor_blocks_cheap_side` alone takes ~50s — a
  short per-test timeout under parallel contention will kill it as a false "worker crash".
- **Pre-existing test baseline (do NOT chase):** a clean checkout of `main` runs at roughly
  **~692 passed / ~68 failed** with `python -m pytest tests/ -n 4 --timeout=300 --timeout-method=thread`
  (~50s). Those failures are stale-baseline assertions in committed code, not an environment problem —
  only investigate NEW failures your changes introduce vs this baseline.
- **No linter** is configured for this plugin (no ruff/flake8/eslint here). A quick syntax check is
  `python -m compileall engine scripts`.
