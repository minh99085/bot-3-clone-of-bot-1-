# Hermes v2 Paper

24/7 **paper-only** Polymarket trading stack ($2000 starting bankroll) with Loop Engineering, pre-trade sizing, Chainlink ground-truth, and a Streamlit desk behind Nginx.

**Dashboard URL:** `http://<VPS_IP>/dashboard`  
Streamlit is **not** exposed on port 8501 publicly — only Nginx `:80` is.

Targets: consistent 80%+ WR · DD &lt; 15% (guard at 8%) · PF &gt; 1.4 · EV after CLOB fees/slippage.

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
python -m backtest                 # synthetic + auto-tighten
python -m backtest --no-auto-tighten
python -m backtest --historical    # Gamma API / cache
pytest tests/test_kelly.py tests/test_bayesian_conviction.py tests/test_enhanced_misprice.py -q
```

### Standalone enhanced paper loop

```bash
export PYTHONPATH=. HERMES_PAPER_ONLY=1
python -m paper_trader
```

(Production 24/7 bot remains `python -m hermes.hermes_loop overnight` — enhanced layer is wired into signal/pretrade/verifier.)

### How to achieve 82%+ win rate (tuning guide)

1. **Keep the model calibrated** — Brier &lt; 0.18 is a hard prerequisite. If live Brier drifts above ~0.18, raise `min_conviction` before increasing size.
2. **Prefer extreme q** — raise `extreme_q_high` to `0.88` and lower `extreme_q_low` to `0.12` in `config/enhanced_misprice.yaml`.
3. **Demand more edge** — set `min_edge: 0.14` (baseline product floor is 0.06; production default is 0.12).
4. **Tighten Beta** — increase `n_eff.crypto` from 80 → 100 so conviction only clears when p is clearly on the wrong side of q.
5. **Shrink Kelly** — `kappa_base: 0.25` (or let DD/WR guards auto-drop to `kappa_guard: 0.20`).
6. **Cut weak buckets** — if WR by edge `0.10–0.15` &lt; 80%, raise `min_edge` until that bucket disappears.
7. **Respect risk budget** — keep `risk_budget: 0.20` and never lift `max_single_market_pct` above `0.10`.
8. **Selectivity over frequency** — fewer high-conviction tickets beat exploring mid-odds; the bandit still probes small when enhanced filters fail.

Synthetic reference (seeded): ~966 trades, **WR ≈ 92.8%**, max DD ≈ 8.6%, Brier ≈ 0.13.

---

## Architecture

| Service | Role |
|---------|------|
| `bot` | Overnight paper loop (Discovery → Handoff/pretrade → Verifier → fill) |
| `dashboard` | Streamlit UI (`baseUrlPath=/dashboard`) |
| `nginx` | Reverse proxy → clean `/dashboard` URL |

```
Browser ──HTTP :80──► nginx ──/dashboard/*──► dashboard:8501
                              (8501 not published)
Bot writes knowledge/ + data/paper/  ◄── shared volumes ──►  Dashboard reads
```

Paper lock: `HERMES_PAPER_ONLY=1` (default). Live orders are refused in broker/executor/CLI.

---

## Quick start (Docker — local)

```bash
cp .env.example .env
docker compose up -d --build

# Desk
open http://localhost/dashboard
# or
curl -fsS http://localhost/healthz

# Logs
docker compose logs -f bot
```

Stop: `docker compose down`

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
