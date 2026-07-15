# SKILL.md — Financial Freedom Bot (Hermes v2)

> Living project skill. Every agent reads this file at the start of a turn.
> The agent forgets; the repo doesn't. Update this file when conventions or
> hard-won incident rules change.

## Mission

Autonomous Polymarket prediction-market trading loop targeting **consistent 80%+ win rate** on settled trades, **max drawdown < 8%**, **profit factor > 1.4**, with positive expectancy after fees and slippage.

Paper mode is default. Live trading is opt-in only after paper evidence clears the gates.

## Identity

| Field | Value |
|-------|-------|
| Codename | Hermes v2 |
| Architecture | Loop Engineering (Addy Osmani) + Roan quant patterns |
| Mode | `paper` until STATE.md says otherwise |
| Verifier model hint | Stronger / different architecture than generator |

## Five Moves (every turn)

1. **Discovery** — find markets worth trading (`discovery.py`)
2. **Handoff** — isolate into worktrees + parquet/JSON handoffs
3. **Verification** — separate checker says PASS/REJECT/DEFER (`verifier.py`)
4. **Persistence** — STATE.md, LESSONS.md, trade ledger
5. **Scheduling** — `@loop` cadence + `@goal` stop conditions (`hermes_loop.py`)

## Six Parts (materials)

| Part | Location |
|------|----------|
| Automations | `@loop` / `@goal` in `hermes/decorators.py` |
| Skills | This file + `ALPHA_RESEARCH_SKILL.md` |
| State / Memory | `STATE.md`, `LESSONS.md`, `data/**/trade_ledger.jsonl` |
| Verifier (sub-agent) | `verifier.py` — assume broken until proven |
| Worktrees | `.worktrees/{research,signal,risk}` |
| Connectors | `connectors/` (Polymarket, CEX, broker, alerts) |

## Hard Rules (we never do X because of Y)

1. **We never execute an unverified signal** — because self-graded generators confidently praise mediocre work (Osmani / Rajasekaran).
2. **We never let the generator model act as verifier** — maker-checker is structural, not a prompt tweak.
3. **We never run `osmani_lane` live** until backtests show WR > 65% and positive EV — Hermes v1 weakness.
4. **We never ignore AVOID entries in LESSONS.md** — the self-improving loop only works if rejects stick.
5. **We never size up into drawdown** — at 4% DD cut size; at 8% hard kill.
6. **We never chase entry** — use `entry_vwap_target` inside the spread; require `pre_entry_stability_ok`.
7. **We never disable the risk monitor** to "get fills" — risk runs in its own worktree and can pause the loop.
8. **We never go live without `HERMES_LIVE=1` AND `live_enabled: true` in STATE.md**.
9. **We never trade confidence tier C or D** — verifier rejects them by policy.
10. **We never clear LESSONS.md** to "start fresh" without archiving — memory is the edge.

## Circuit Breakers

- Max drawdown: **8%**
- Max daily loss: **3%** of capital
- Max consecutive losses: **4**
- Rolling WR(20) soft pause: **< 55%**
- Rolling PF(20) soft pause: **< 1.2**
- Max single position: **3%** of capital
- Max open exposure: **20%** of capital

## Human Inbox

Anything the loop cannot confidently handle goes to `data/paper/human_inbox.jsonl` (or live twin). Check the inbox every morning. Do not bypass DEFER by forcing PASS.

## Model Choices (suggested)

| Role | Model bias | Why |
|------|------------|-----|
| Discovery / signal generator | Fast mid-tier (Sonnet-class / GPT mid) | Throughput; many candidates |
| Verifier | **Stronger / different family** (Opus-class / o-series) | Skeptical evaluator; different blind spots |
| Lessons distillation | Mid-tier with structured output | Cheap, frequent writes |
| Risk monitor | Deterministic code first; LLM only for narrative | Numbers don't need an LLM |

## Conventions

- All money in USD; all prices in [0, 1] for binary markets.
- Timestamps UTC ISO-8601.
- Handoffs: `data/handoff/{stage}_{turn_id}_{stamp}.json` (+ parquet when available).
- Logs: structured via stdlib logging; turn summaries in `data/paper/turns.jsonl`.

## Auto-Promoted Rules

<!-- lessons_engine appends below this heading -->
