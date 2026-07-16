# Hermes v2 Paper

24/7 **paper-only** Polymarket trading stack ($2000 starting bankroll) with Loop Engineering, pre-trade sizing, Chainlink ground-truth, and a Streamlit desk behind Nginx.

**Dashboard URL:** `http://<VPS_IP>/dashboard`  
Streamlit is **not** exposed on port 8501 publicly — only Nginx `:80` is.

Targets: consistent 80%+ WR · DD &lt; 15% (guard at 8%) · PF &gt; 1.4 · EV after CLOB fees/slippage.

---

## Quick Start — Validate 80% Win Rate

**Step 1 — install**

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export PYTHONPATH=.
```

**Step 2 — Validate 80%+ Win Rate with Backtest** (do this before trusting paper trades)

```bash
# Recommended first command (< 2 minutes)
python -m backtest --fast
```

You should see a green verdict like:

```text
✅ Target met: 88.x% win rate on N trades | Monte Carlo 5th percentile: … | Max DD: …
```

Full guide (what every number means, plots, tuning): **[BACKTEST_GUIDE.md](BACKTEST_GUIDE.md)**.

```bash
# Full synthetic validation + naive vs enhanced comparison
python -m backtest --n-markets 5000 --seed 42 --compare-baseline

# Find & save best thresholds (≥80% WR)
python -m backtest --optimize --n-markets 5000
```

Same entrypoint: `python backtest/run.py --fast`

---

## Enhanced misprice stack (Kelly + Beta + risk budget)

Wraps Option D CEX↔Polymarket mispricing with exact math in:

| Package | Role |
|---------|------|
| `strategy/enhanced_misprice.py` | Hard filters + ranking (wraps `hermes.mispricing`) |
| `strategy/kelly.py` | Polymarket Kelly: YES `(q-p)/(1-p)`, NO `(p-q)/p`, `f=κ·min(f*,1)` |
| `strategy/bayesian.py` | Beta conviction via `scipy.stats.beta` |
| `risk/portfolio_risk.py` | Risk units + DD/WR guards + early exit |
| `backtest/` | Synthetic + Gamma historical + reporting |
| `paper_trader/` | Slippage fills + async loop |
| `config/enhanced_misprice.yaml` | All thresholds tunable |

### Run backtest (must clear ≥80% WR when Brier &lt; 0.18)

```bash
export PYTHONPATH=.
python -m backtest --fast
python -m backtest --filter-mode strict_real --fast   # high WR, real cex_implied_up as q
python -m backtest --filter-mode moderate --fast      # more trades, looser real-q gates
python -m backtest --n-markets 5000 --seed 42 --compare-baseline
python -m backtest --optimize --n-markets 5000
python -m backtest monte-carlo --n_runs 50
pytest tests/test_kelly.py tests/test_bayesian_conviction.py tests/test_enhanced_misprice.py tests/test_backtest_suite.py tests/test_filter_modes.py -q
```

### Filter modes (`strict` / `strict_real` / `moderate` / `aggressive`)

Set in `config/enhanced_misprice.yaml` or via `--filter-mode`:

| Mode | Intent | Key thresholds | Notes |
|------|--------|----------------|-------|
| `strict` | Max WR, fewest trades (legacy) | edge 0.12 · conv 0.95 · q∈{≥0.88,≤0.12} · κ 0.35 · max 10% | Extreme-q band from inflated-q era |
| `strict_real` (active) | High WR with **real** `cex_implied_up` as q | edge 0.14 · conv 0.93 · q∈{≥0.85,≤0.15} · κ 0.35 · max 8% · risk 0.18 | Targets ≥80% WR; cuts weak edge&lt;0.15 buckets |
| `moderate` | More fills, looser real-q gates | edge 0.085 · conv 0.88 · q∈{≥0.80,≤0.20} · κ 0.40 · max 9% | VPS 2026-07-16: ~58% WR under real q |
| `aggressive` | Highest frequency | edge 0.12 · conv 0.93 · q∈{≥0.85,≤0.15} · κ 0.30 · max 8% | ~82–83% WR on synthetic (inflated-q era) |

Under real q, keep `min_edge ≥ 0.14` — VPS buckets below ~0.15 destroy win rate.

### Standalone enhanced paper loop

```bash
export PYTHONPATH=. HERMES_PAPER_ONLY=1
python -m paper_trader
```

(Production 24/7 bot remains `python -m hermes.hermes_loop overnight` — enhanced layer is wired into signal/pretrade/verifier.)

### How to achieve 82%+ win rate (tuning guide)

1. **Start with a mode** — `mode: strict_real` for high WR with real q; `mode: moderate` for more fills; `mode: strict` only if revisiting legacy inflated-q backtests; `mode: aggressive` for frequency.
2. **Keep the model calibrated** — Brier &lt; 0.18 is a hard prerequisite. If live Brier drifts above ~0.18, raise `min_conviction` before increasing size.
3. **Prefer extreme q** — raise `extreme_q_high` / lower `extreme_q_low`, or switch toward `strict_real`.
4. **Demand more edge** — under real q keep `min_edge ≥ 0.14` (VPS: only edge ≥0.15 stayed profitable). Do not loosen toward moderate without a fresh backtest.
5. **Tighten Beta** — increase `n_eff.crypto` from 80 → 100 so conviction only clears when p is clearly on the wrong side of q.
6. **Shrink Kelly** — `kappa_base: 0.25` (or let DD/WR guards auto-drop to `kappa_guard: 0.20`).
7. **Cut weak buckets** — if WR by edge `0.10–0.15` &lt; 80%, raise `min_edge` until that bucket disappears.
8. **Respect risk budget** — `strict_real` uses `risk_budget: 0.18` and `max_single_market_pct: 0.08`; never lift max single above `0.10`.
9. **Selectivity over frequency** — fewer high-conviction tickets beat exploring mid-odds; the bandit still probes small when enhanced filters fail.

Synthetic reference (seeded, strict): ~813 trades, **WR ≈ 91%**, max DD &lt; 15%, Brier ≈ 0.14.

---

## Architecture — 5 isolated instances ($2k each = $10k fleet)

| Service | MARKET_FILTER | Bankroll | Logs / paper data |
|---------|---------------|----------|-------------------|
| `hermes-btc5` | `btc5` | $2000 | `logs/btc5/`, `data/paper/btc5/` |
| `hermes-btc15` | `btc15` | $2000 | `logs/btc15/`, `data/paper/btc15/` |
| `hermes-eth5` | `eth5` | $2000 | `logs/eth5/`, `data/paper/eth5/` |
| `hermes-sol5` | `sol5` | $2000 | `logs/sol5/`, `data/paper/sol5/` |
| `hermes-rotator` | `rotator` | $2000 | `logs/rotator/` — scans all four, **1** highest-conviction trade/turn |
| `dashboard` | — | — | Aggregates all instance ledgers |
| `nginx` | — | — | Public `/dashboard` |

Patterns live in **`config/market_filters.yaml`**. Win-rate knobs (`min_edge`, `min_conviction`, Kelly, risk) stay in `config/enhanced_misprice.yaml` and are **unchanged**.

```
Browser ──HTTP :80──► nginx ──/dashboard/*──► dashboard:8501
5× bots write data/paper/<id>/ + logs/<id>/  ◄── shared volumes ──►  Dashboard reads
```

Paper lock: `HERMES_PAPER_ONLY=1` (default). Live orders are refused in broker/executor/CLI.

---

## Quick start (Docker — all 5 instances)

```bash
cp .env.example .env
# Creates hermes-btc5, hermes-btc15, hermes-eth5, hermes-sol5, hermes-rotator + dashboard + nginx
docker compose up -d --build

# Desk
open http://localhost/dashboard
curl -fsS http://localhost/healthz

# Per-instance logs
docker compose logs -f hermes-btc5
docker compose logs -f hermes-rotator

# Status
docker compose ps
```

Stop: `docker compose down`

Each container injects `MARKET_FILTER` + isolated `HERMES_LOG_DIR` / `HERMES_PAPER_DIR`. To run a single lane locally without Docker:

```bash
export PYTHONPATH=. HERMES_PAPER_ONLY=1
export HERMES_INSTANCE_ID=btc5 MARKET_FILTER=btc5
export HERMES_LOG_DIR=logs/btc5 HERMES_PAPER_DIR=data/paper/btc5
python -m hermes.hermes_loop overnight
```

---

## Quick start (Python — no Docker)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export PYTHONPATH=. HERMES_PAPER_ONLY=1 HERMES_LIVE=0
python -m hermes.hermes_loop overnight
# other terminal:
streamlit run dashboard.py --server.baseUrlPath=dashboard
```

---

## VPS deploy (exact steps)

**Host:** `207.246.96.45` (or your IP) · **Path:** `/opt/financial-freedom-bot`

### Option A — one-shot deploy script

From your laptop / cloud agent (SSH key at `~/.ssh/bot3_cloud_agent`):

```bash
git push -u origin main
./deploy/deploy_vps.sh
```

This rsyncs the repo, then on the VPS runs:

```bash
docker compose down --remove-orphans
docker compose up -d --build --remove-orphans
```

Opens **UFW 80 + SSH only** (not 8501), installs the systemd unit, and starts the stack.

### Option B — manual on the VPS

```bash
# 1) Clone / sync code
mkdir -p /opt/financial-freedom-bot && cd /opt/financial-freedom-bot
# (rsync or git pull)

# 2) Env
cp .env.example .env
# edit if needed — keep HERMES_PAPER_ONLY=1 and HERMES_LIVE=0
mkdir -p data/paper data/handoff logs

# 3) Firewall (critical: do NOT expose 8501)
ufw allow OpenSSH
ufw allow 80/tcp
ufw --force enable

# 4) Start via systemd (auto-restart on boot / failure)
cp deploy/hermes-paper.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now hermes-paper.service

# 5) Verify
docker compose ps
curl -fsS http://127.0.0.1/healthz
curl -fsS http://127.0.0.1/dashboard/_stcore/health
```

### Access

| URL | Purpose |
|-----|---------|
| `http://<VPS_IP>/dashboard` | Paper trading desk |
| `http://<VPS_IP>/healthz` | Nginx liveness |

Containers restart with `restart: unless-stopped`. systemd brings the compose stack back after reboot.

```bash
journalctl -u hermes-paper -f
docker compose -f /opt/financial-freedom-bot/docker-compose.yml logs -f
```

---

## Dashboard contents

Auto-refresh every 5 minutes. Shows:

- Equity curve from **$2000** + total PnL  
- Open positions / exposure  
- Recent trades + reasons  
- Sub-strategy WR / EV / allocation weight / trend  
- Portfolio metrics (div ratio, HHI, CUT/REDUCE)  
- Latest lessons from `LESSONS.md`  
- Chainlink prices + alignment  
- Pre-trade sizing decisions  

---

## Pre-trade sizing + self-learning (how 80%+ WR is defended)

Handoff is portfolio-aware, not fixed notional:

1. **Allocation** — Ledoit-Wolf → HRP / edge-RP → Black-Litterman → cut/reduce  
2. **Pre-trade** (`hermes/pretrade.py`) per signal:
   - Sleeve stats from the ledger  
   - Binding rules from `LESSONS.md`  
   - Live EV from **CLOB book + Chainlink alignment**  
   - Portfolio impact (diversification / concentration)  
   - Output **% of $2000 bankroll** (max 3%) or **0% skip**  
3. **Verifier** must approve **signal quality and size** (rejects `pretrade_skip`)  
4. Decisions → `data/paper/pretrade_decisions.jsonl` → dashboard  
5. Settlements / rejects → `lessons_engine` → `LESSONS.md` → next turn sizing  

```
Discovery → Signals → HRP/BL → Pre-trade size% → Verifier → Paper fill (CLOB sim)
                                              ↓
                                   LESSONS + STATE + ledger → /dashboard
```

---

## Config (environment)

See `.env.example`. Important knobs:

| Var | Default | Meaning |
|-----|---------|---------|
| `HERMES_PAPER_ONLY` | `1` | Hard paper lock |
| `HERMES_CAPITAL` | `2000` | Starting bankroll USD |
| `HERMES_INTERVAL` | `300` | Seconds between turns |
| `HERMES_HTTP_PORT` | `80` | Host port for nginx |
| `CHAINLINK_API_KEY` | — | Optional Data Streams |

Structured logs: `logs/hermes-bot.log`, `logs/heartbeat.json`.

---

## Loop Engineering (5×6)

| Move | Module |
|------|--------|
| Discovery | `discovery.py` + hybrid Chainlink/CLOB |
| Handoff | `portfolio.py` + `pretrade.py` |
| Verification | `verifier.py` (signal + size + oracle) |
| Persistence | `STATE.md` / `LESSONS.md` / ledgers |
| Scheduling | `@loop` overnight in `hermes_loop.py` |

Connectors: `py-clob-client-v2`, Chainlink feeds/streams, paper broker.

---

## Health checks

- **Bot:** HTTP `:8080/health` + `logs/heartbeat.json` (Docker healthcheck)  
- **Dashboard:** `GET /dashboard/_stcore/health`  
- **Nginx:** `GET /healthz`  

---

## Tests

```bash
pip install -r requirements.txt pytest
PYTHONPATH=. pytest -q
```

Git workflow: **always push to `main` → sync VPS → `compose down --remove-orphans` → `up -d --build --remove-orphans`** (via `./deploy/deploy_vps.sh`).
