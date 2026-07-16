# AGENTS.md

## Cursor Cloud specific instructions

**Hermes Agent v3 Paper** — 24/7 Polymarket paper stack locked to **consistent ≥80% WR** under real `cex_implied_up` as q. Dashboard: `http://<VPS_IP>/dashboard`.

### Non-negotiable performance targets

Gold standard: `reports/full_backtest_vps_20260716_strict_real` (89.7% WR, MC p5 85.3%, DD 8.0%).

| Gate | Requirement |
|------|-------------|
| Win rate | ≥ 80% (target mean ≥ 87%) |
| Monte Carlo (≥20 seeds) | p5 WR ≥ 82%, mean ≥ 87% |
| Max drawdown | ≤ 8% (soft/hard guard at 8%; absolute lockout 15%) |
| Profit factor | ≥ 2.5 (after simulated fees + slippage) |
| Selectivity | 4–10% |
| Brier | ≤ 0.15 |
| Mode | `strict_real` only for production |
| Paper lock | `HERMES_PAPER_ONLY=1`, `HERMES_LIVE=0` |

**Frozen filters** (`STRICT_REAL_FREEZE` in `models/config.py`) — do **not** loosen:

```yaml
mode: strict_real
min_edge: 0.14          # CRITICAL — below this destroys WR under real q
min_conviction: 0.93
min_conviction_guard: 0.96
extreme_q_high: 0.85    # synthetic / when model q is already extreme
extreme_q_low: 0.15
extreme_anchor: q
extreme_p_high: 0.72    # live real-q only: fade stretched Polymarket YES
extreme_p_low: 0.28
kappa_base: 0.35
kappa_guard: 0.20
max_single_market_pct: 0.08
risk_budget: 0.18
dd_guard_pct: 0.08
max_drawdown_hard_pct: 0.15
```

Never restore artificial extreme-q push (0.97/0.03). Model q = live `cex_implied_up` (lightly smoothed). Live path sets `live_real_q=True` so mid CEX q uses `extreme_p_*` on Polymarket — requiring q≥0.85 live dead-stops the desk.

### VPS baseline

- **Host:** `207.246.96.45` (user `root`)
- **Deploy path:** `/opt/financial-freedom-bot`
- **SSH:** `~/.ssh/bot3_cloud_agent` (or `BOT3_VPS_SSH_PRIVATE_KEY`)
- **Deploy:** `./deploy/deploy_vps.sh` (Docker Compose + nginx + systemd)
- **Public ports:** **80 only** (nginx). Do **not** expose 8501.

### VM baseline (cloud agent environment)

- Python 3.12
- Node.js v22
- Docker available on VPS

### Install / run (keep in sync with README)

```bash
# Local Python
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export PYTHONPATH=. HERMES_PAPER_ONLY=1

# Validate Hermes v3 gates first
python -m backtest --filter-mode strict_real --fast
python -m backtest --n-markets 5000 --seed 42 --compare-baseline

python -m hermes.hermes_loop overnight
streamlit run dashboard.py --server.baseUrlPath=dashboard
pytest -q

# Docker (preferred for VPS)
cp .env.example .env
docker compose up -d --build
# → http://localhost/dashboard
```

### Git + VPS deploy workflow (mandatory)

After every code change, always do this sequence — no feature branches, no PRs unless asked:

1. **Commit and push to `main`**
   ```bash
   git add -A && git commit -m "..." && git push -u origin main
   ```
2. **Sync VPS** (rsync via `./deploy/deploy_vps.sh`)
3. **On VPS: compose down, remove orphans, rebuild**
   ```bash
   docker compose down --remove-orphans
   docker compose up -d --build --remove-orphans
   ```

`./deploy/deploy_vps.sh` performs steps 2–3 automatically after a successful SSH check.

When a deploy round is finished, end with: **I am done thinking, push to main vps and rebuild**

### Architecture pointers

- Living skills: `knowledge/SKILL.md`, `ALPHA_RESEARCH_SKILL.md`
- Memory: `knowledge/STATE.md`, `LESSONS.md`
- Verifier is sacred: `hermes/verifier.py`
- **Paper lock:** `HERMES_PAPER_ONLY=1` — live trading disabled in this deployment
- **Filter lock:** `mode: strict_real` — never ship `moderate`/`aggressive` as production
- **Market scope:** 5 instances via `MARKET_FILTER` (`btc5` / `btc15` / `eth5` / `sol5` / `rotator`) — see `config/market_filters.yaml`
- **Fleet capital:** 5 × $2000 = $10k (isolated bankrolls + ledgers under `data/paper/<id>/`)
- Compose: `hermes-btc5` + `hermes-btc15` + `hermes-eth5` + `hermes-sol5` + `hermes-rotator` + `dashboard` + `nginx`

### Advanced multi-signal ensemble (q quality — strict_real freeze unchanged)

Replaces toy `momentum → cex_implied_up` when CEX tick history is available:

| Module | Role |
|--------|------|
| `strategy/advanced_signals.py` | Hurst-gated multi-TF slopes + OBI/IR/VAMP + GARCH log-normal + OU + Kalman fusion |
| `strategy/signal_calibration.py` | Rolling Brier/WR → re-fit swarm/market blend after N settlements |
| `hermes/mispricing.py` | Calls ensemble; falls back to momentum when history is thin |
| `config/enhanced_misprice.yaml` → `advanced:` | Tunables (defaults keep current WR) |

- Env kill-switch: `HERMES_ADVANCED_SIGNALS=0`
- Zero-config overnight: thin history → same toy map as before; hard filters / Kelly / Bayesian / `live_real_q` untouched
- Prove ensemble vs momentum: `python -m backtest --fast --advanced-features`

### Autonomy stack (self-adjust — freeze-safe)

Package `autonomy/` — MCHB, CBPF, EHO, RASP, RGMC, data lifecycle, model registry.

```bash
python -m autonomy.bootstrap          # one-shot data + pretrain
python -m autonomy.continuous         # forever (or use hermes overnight — autonomy_tick wired)
```

- Skills: `knowledge/skills/self_improve.md`, `data_ingest.md`, `risk_guardian.md`, `mchb.md`
- **Never** mutates `STRICT_REAL_FREEZE`. Soft knobs only (`swarm_weight`, size×, soft κ scale ≤1).
- Shadow ≥100 paper trades @ ≥80% WR before promote; auto-rollback if live WR &lt; 78%.
