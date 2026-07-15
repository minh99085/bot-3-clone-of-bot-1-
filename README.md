# Financial Freedom Bot — Hermes v2

Autonomous Polymarket prediction-market trading loop built on **Loop Engineering** (Addy Osmani) and Roan-style quant patterns. Paper-first. Self-improving. Verifier-gated.

**Targets:** 80%+ win rate on settled trades · max drawdown &lt; 8% · profit factor &gt; 1.4 · positive expectancy after fees/slippage.

---

## Five Moves × Six Parts

| Move | What happens | Hermes module |
|------|----------------|---------------|
| **Discovery** | Find markets worth trading (no human task list) | `hermes/discovery.py` |
| **Handoff** | Isolate work (git worktrees + JSON/parquet) | `hermes/worktrees.py`, `data/handoff/` |
| **Verification** | Separate checker says NO by default | `hermes/verifier.py` |
| **Persistence** | Memory on disk, not in the context window | `knowledge/*`, trade ledger |
| **Scheduling** | Cadence + stop conditions | `@loop` / `@goal` in `hermes_loop.py` |

| Part | Material |
|------|----------|
| Automations | `@loop(interval=...)`, `@goal(...)` |
| Skills | `knowledge/SKILL.md`, `ALPHA_RESEARCH_SKILL.md` |
| Memory | `STATE.md`, `LESSONS.md`, `data/**/trade_ledger.jsonl` |
| Sub-agents (maker-checker) | Generator ≠ Verifier (different instructions + stronger model hint) |
| Worktrees | `.worktrees/{research,signal,risk}` |
| Connectors | `connectors/` — Polymarket, CEX, broker, Slack/Telegram |

> The hard part of a loop isn't the loop — it's putting something inside it that can say **no**. That something is `verifier.py`.

---

## Quick start (paper)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export PYTHONPATH=.

# One turn
python -m hermes.hermes_loop once

# Full demo: turn + settlement + lesson
python -m hermes.hermes_loop demo
# or
python examples/full_loop_turn.py

# Overnight (main loop 5m, risk monitor 30s)
python -m hermes.hermes_loop overnight --interval 300

# Convenience
chmod +x scripts/*.sh deploy/*.sh
./scripts/run_paper.sh once
./scripts/run_paper.sh demo
```

### Enable live (only after paper evidence)

1. Paper settled WR ≥ 80%, PF &gt; 1.4, DD &lt; 8% over a meaningful sample.
2. Set `**Live Enabled**: true` in `knowledge/STATE.md`.
3. `export HERMES_LIVE=1`
4. `./scripts/run_live.sh`

Live broker path raises until CLOB/wallet MCP is wired — by design.

---

## One full loop turn

```
┌─────────────┐   handoff    ┌──────────────────┐   handoff   ┌────────────┐
│  Discovery  │ ──────────►  │ Signal Generator │ ──────────► │  Verifier  │
│  (skill)    │  candidates  │ (ALPHA skill)    │  signals    │ (checker)  │
└─────────────┘              └──────────────────┘             └─────┬──────┘
                                                                    │
                     REJECT/DEFER → LESSONS.md                      │ PASS only
                     + human_inbox.jsonl                            ▼
                                                            ┌────────────┐
                                                            │  Executor  │
                                                            │ (paper/live)│
                                                            └─────┬──────┘
                                                                  │ settle
                                                                  ▼
                                                            ┌────────────────┐
                                                            │ Lessons Engine │
                                                            │ → SKILL promote│
                                                            └────────────────┘

Parallel: risk_monitor @ 30s in .worktrees/risk ──► can set Pause Loop in STATE.md
```

### Verifier gates (all must pass)

1. Historical edge in exact bucket/mode/regime above threshold (WR ≥ 65%, n ≥ 20, PF ≥ 1.4)
2. Live EV after fees + slippage ≥ **0.06** (prefer 0.08)
3. Regime filter + conviction ≥ 0.55
4. Not on any `AVOID:` rule from `LESSONS.md`
5. Sizing respects drawdown + correlation caps
6. Tier A/B only; lane not GATED/KILLED
7. `pre_entry_stability_ok` + `entry_vwap_target` present

### Hermes v1 weaknesses addressed

| Weakness | Fix |
|----------|-----|
| `osmani_lane` bleed | GATED until backtest WR &gt; 65% +EV |
| Weak regime/hour/tier guards | First-class in discovery + verifier |
| Execution drag | Tighter VWAP + stability filter |
| Implicit DOWN bias | Explicit dynamic bias from STATE |
| No performance pause | Daily/rolling gates in `risk_monitor` |

---

## Model choices

| Role | Suggestion | Why |
|------|------------|-----|
| Signal generator | Mid-tier (Sonnet-class) | Throughput |
| **Verifier** | **Stronger / different family** (Opus / o-series) | Skeptical; different blind spots |
| Lessons | Mid-tier structured JSON | Cheap frequent writes |
| Risk | Deterministic code | Numbers don't need an LLM |

Wire LLM backends later behind `GENERATOR_MODEL` / `VERIFIER_MODEL`; numeric gates already run without an API key so overnight paper works offline.

---

## Repo layout

```
hermes/           # core loop modules
connectors/       # Polymarket, CEX, broker, alerts
knowledge/        # SKILL, ALPHA, STATE, LESSONS (living layer)
config/           # hermes.yaml, risk_limits.yaml
data/             # paper/live ledgers + handoffs (gitignored contents)
scripts/          # run_paper.sh, run_live.sh
deploy/           # deploy_vps.sh → /opt/financial-freedom-bot
examples/         # full_loop_turn.py
tests/            # verifier + lessons + discovery
```

---

## Deploy (VPS)

Host default: `207.246.96.45` → `/opt/financial-freedom-bot`

```bash
./deploy/deploy_vps.sh
```

Install on VPS matches README: `python3 -m venv .venv && pip install -r requirements.txt`.

---

## Tests

```bash
PYTHONPATH=. pytest -q
```

---

## Safety

- Circuit breakers in `risk_monitor.py` + `SKILL.md`
- Human inbox: `data/paper/human_inbox.jsonl`
- Every decision logged with turn handoffs under `data/handoff/`
- Live double-gated (`HERMES_LIVE` + STATE.md)

Stay the engineer outside the loop: tune skills, thresholds, and the verifier — not individual prompts each morning.
