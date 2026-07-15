# Financial Freedom Bot — Hermes v2

Autonomous Polymarket (BTC/ETH up-down + peers) trading loop: **Loop Engineering** (Osmani) + Roan self-improving skills + **Ruuj robust portfolio construction**. Paper-first. Verifier-gated. Allocation-aware.

**Targets:** consistent 80%+ WR on settled trades · DD &lt; 8% · PF &gt; 1.4 · positive EV after fees/slippage.

The path from ~62% fragility to stable 80%+ is the triad: **Verifier + Lessons Engine + Portfolio Allocation Layer**.

---

## Five Moves × Six Parts × Portfolio Layer

| Move | What happens | Module |
|------|----------------|--------|
| **Discovery** | Find markets / sleeves worth trading | `discovery.py` |
| **Handoff** | Worktrees + **HRP/BL sizing** of opportunities | `worktrees.py`, **`portfolio.py`** |
| **Verification** | Checker approves **signal AND allocation** | `verifier.py` |
| **Persistence** | STATE (incl. portfolio metrics), LESSONS, ledger | `knowledge/*` |
| **Scheduling** | `@loop` cadence + `@goal` stops | `hermes_loop.py` |

| Part | Material |
|------|----------|
| Automations | `@loop` / `@goal` |
| Skills | `SKILL.md`, `ALPHA_RESEARCH_SKILL.md` (alpha **+ allocation** rules) |
| Memory | `STATE.md`, `LESSONS.md` (drives signal **and** weight heuristics) |
| Verifier | Separate stronger-model checker — assume broken until proven |
| Worktrees | `.worktrees/{research,signal,risk}` |
| Connectors | Polymarket, CEX, broker, alerts |

### Sub-strategies = return sources

Every unique `(market_series | entry_mode | regime | hourly_bucket)` is a portfolio sleeve. Capital is allocated across sleeves, not “the latest signal.”

---

## Portfolio Construction (wired into the loop)

```
Settlements → return matrix
     → Ledoit-Wolf shrinkage (never raw sample cov)
     → HRP base  (or edge-weighted RP if T small)
     → Black-Litterman tilt (Grok / TV / conviction views)
     → Cut/Reduce caps (internal confidence)
     → sized signals in Handoff
     → Verifier must approve size/weight
```

| Piece | Behavior |
|-------|----------|
| **Robust base** | LW cov + HRP / edge-RP |
| **Dynamic sizing** | Edge quality × diversification × sleeve health |
| **BL views** | Low conf barely moves weights; high conf tilts |
| **Cut / Reduce** | `model_broken` → CUT; `currently_losing` → REDUCE |
| **Self-improve** | Lessons promote `CUT:`/`REDUCE:` into ALPHA skill |

---

## Quick start (paper)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export PYTHONPATH=.

python -m hermes.hermes_loop demo       # discovery→alloc→verify→fill→lesson
python -m hermes.hermes_loop overnight  # 5m main + 30s risk
pytest -q
```

Live: paper evidence first, then `Live Enabled: true` in STATE + `HERMES_LIVE=1`.

---

## One full turn

```
Discovery → Signals → Portfolio Handoff (LW/HRP/BL/cut)
                           ↓
                     Verifier (signal + allocation)
                           ↓
              PASS → Executor    REJECT → LESSONS (+ alloc rules)
                           ↓
                     Settlement → Lessons → update ALPHA allocation heuristics
```

Risk monitor @ 30s in its own worktree can pause the loop (DD, daily loss, rolling WR/PF).

---

## Verifier gates (signal + allocation)

1. Bucket WR ≥ 65%, n ≥ 20, PF ≥ 1.4  
2. Live EV ≥ 0.06 after fees/slippage  
3. Regime + conviction; tier A/B only  
4. Not on AVOID; lane not gated  
5. Entry VWAP + stability  
6. DD / correlation sizing  
7. **Allocation approved** — not CUT, size &gt; 0, HHI / div-ratio OK  

---

## Model choices

| Role | Bias |
|------|------|
| Generator | Mid-tier throughput |
| **Verifier** | **Stronger / different family** |
| Allocation | Deterministic (numpy) — no LLM required |
| Lessons | Mid-tier structured writes |

---

## Repo layout

```
hermes/
  hermes_loop.py      # orchestrator
  portfolio.py        # LW + HRP + BL + sizing
  substrategy.py      # sleeve IDs + cut/reduce confidence
  verifier.py         # signal + allocation checker
  lessons_engine.py   # signal + allocation lessons
  ...
knowledge/            # SKILL, ALPHA (allocation rules), STATE, LESSONS
```

---

## Safety

Circuit breakers, human inbox, full handoff logs, live double-gate. Stay outside the loop: tune skills, thresholds, and the verifier — not individual morning prompts.

Git: **commit and push directly to `main`** (no feature branches).
