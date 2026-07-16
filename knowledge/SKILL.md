# SKILL.md — Financial Freedom Bot (Hermes Agent v3)

> Living project skill. Every agent reads this file at the start of a turn.
> The agent forgets; the repo doesn't. Update this file when conventions or
> hard-won incident rules change.

## Mission

Autonomous Polymarket paper-trading loop targeting **consistent ≥80% win rate**
(Monte Carlo p5 ≥ 82%, mean ≥ 87%) under **real `cex_implied_up` as q**, with
**max drawdown ≤ 8%**, **profit factor ≥ 2.5**, selectivity 4–10%, Brier ≤ 0.15.

Gold standard: `reports/full_backtest_vps_20260716_strict_real`.

Paper mode is default. Live trading is opt-in only after paper evidence clears the gates.

## Identity

| Field | Value |
|-------|-------|
| Codename | Hermes Agent v3 |
| Architecture | Loop Engineering (Osmani) + Roan + Ruuj + Chainlink/Polymarket |
| Filter mode | `strict_real` (frozen — see `STRICT_REAL_FREEZE`) |
| Mode | `paper` until STATE.md says otherwise |
| Verifier model hint | Stronger / different architecture than generator |
| Allocation | Ledoit-Wolf → HRP/edge-RP → Black-Litterman → cut/reduce |
| Data | Polymarket Gamma+CLOB (`py-clob-client-v2`) + Chainlink oracles |

## Five Moves (every turn)

1. **Discovery** — find markets worth trading (`discovery.py`)
2. **Handoff** — worktrees + **portfolio allocation** + **pre-trade sizing** (% of bankroll)
3. **Verification** — checker approves **signal + size** (`verifier.py`)
4. **Persistence** — STATE.md (incl. portfolio metrics), LESSONS.md, ledger
5. **Scheduling** — `@loop` cadence + `@goal` stop conditions (`hermes_loop.py`)

## Six Parts (materials)

| Part | Location |
|------|----------|
| Automations | `@loop` / `@goal` in `hermes/decorators.py` |
| Skills | This file + `ALPHA_RESEARCH_SKILL.md` |
| State / Memory | `STATE.md`, `LESSONS.md`, `data/**/trade_ledger.jsonl` |
| Verifier (sub-agent) | `verifier.py` — assume broken until proven |
| Worktrees | `.worktrees/{research,signal,risk}` |
| Connectors | `connectors/` Polymarket CLOB, Chainlink, hybrid, broker, alerts |

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
11. **We never size with raw sample covariance** — Ledoit-Wolf only (Ruuj).
12. **We never confuse currently_losing with model_broken** — REDUCE vs CUT.
13. **We never skip allocation verification** — size/weight must clear HHI + div gates.
14. **We never trust a single CEX tick for BTC/ETH HF** — Chainlink is ground-truth; CLOB is the book.
15. **We never PASS 5m/15m crypto when oracle is stale or misaligned** with Polymarket YES pricing.
16. **We never skip pre-trade analysis** — size is % of bankroll (max 3%) or 0% skip; verifier rejects `pretrade_skip`.
17. **We never expose Streamlit :8501 publicly** — Nginx serves `http://<IP>/dashboard` only.
18. **Hermes Paper deployments keep `HERMES_PAPER_ONLY=1`** — no live orders on the VPS stack.
19. **We only scan/trade scoped fast crypto lanes** via `MARKET_FILTER` (`btc5` / `btc15` / `eth5` / `sol5` / `rotator`).
20. **We start small on fast markets** — 0.5% bankroll cold-start; scale only when lessons show WR/EV improving.
21. **We never loosen `strict_real` below `min_edge: 0.14`** — weaker edge buckets destroy WR under real q.
22. **We never reintroduce artificial extreme-q push (0.97/0.03)** — model q is live `cex_implied_up` only (advanced ensemble when CEX history exists; momentum fallback otherwise).
23. **Advanced ensemble** (`strategy/advanced_signals.py`) improves q quality only — never loosen `STRICT_REAL_FREEZE` gates.
24. **We never ship `moderate` / `aggressive` as production filter mode** — research only; production is `strict_real`.
25. **We never mark a full backtest green unless Hermes v3 gates clear** — WR≥80%, MC p5≥82%, DD≤8%, PF≥2.5, Brier≤0.15.
26. **Autonomy may only mutate soft knobs** — never `min_edge` / `min_conviction` / `strict_real` / κ base / risk budget. See `knowledge/skills/self_improve.md`.
27. **Shadow → prod requires 100 paper trades at ≥80% WR**; rollback if live rolling WR &lt; 78%.

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
