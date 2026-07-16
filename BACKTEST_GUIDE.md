# Backtest Guide — Validate 80%+ Win Rate (Beginner Friendly)

You do **not** need any backtesting experience. This guide gets you from zero to a clear
pass/fail on the Hermes 80% win-rate target.

---

## Quick Start – Validate 80%+ Win Rate in < 2 minutes

```bash
# one-time setup
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export PYTHONPATH=.
```

### 1) Fast first run (recommended)

```bash
python -m backtest --fast
```

or:

```bash
python backtest/run.py --fast
```

What you should see at the end:

```text
✅ Target met: 88.x% win rate on N trades | Monte Carlo 5th percentile: 8x.x% | Max DD: x.x%
```

Artifacts are saved under `artifacts/backtest_runs/YYYYMMDD_HHMMSS/`.

### 2) Full synthetic validation (5,000+ markets)

```bash
python -m backtest --n-markets 5000 --seed 42 --compare-baseline
```

This is the rigorous check: more markets, plots on by default, and a side-by-side
comparison vs naive misprice-only trading.

### 3) Parameter optimization run

```bash
python -m backtest --optimize --n-markets 5000
```

Finds thresholds that clear WR ≥ 80% and DD ≤ 15%, then writes:

- `config/best_params.json`
- `config/best_params.yaml`
- updates `config/enhanced_misprice.yaml` with the winning values

Fast optimize (for a quick search):

```bash
python -m backtest --optimize --fast
```

---

## What the Numbers Mean

| Number | Plain English | Good look |
|--------|---------------|-----------|
| **Win rate** | Of the trades the bot *actually took*, how many made money? | ≥ **80%** |
| **Trades taken** | How many bets passed every filter | Dozens–thousands (not zero) |
| **Selectivity** | Trades ÷ decision points looked at | Often **~5%** — picky is good |
| **Max drawdown (DD)** | Worst drop from a peak in the equity curve | ≤ **15%** (hard cap) |
| **Profit factor** | Gross wins ÷ gross losses | > 1.0 (higher is better) |
| **Expectancy** | Average dollars per trade | Should be positive |
| **Brier score** | How wrong the probability model is | < **0.18** or the WR target is unreliable |
| **Monte Carlo 5th percentile** | In unlucky random universes, what’s the WR floor? | Comfortably ≥ **75–78%** for “consistent” |
| **Conviction buckets** | WR inside groups of Beta-confidence | Higher conviction should win more often |

**Verdict line** (printed at the end of every run) packs the essentials into one sentence so you don’t have to hunt.

---

## Filter modes (strict / strict_real / moderate / aggressive)

Hermes ships four entry-filter profiles in `config/enhanced_misprice.yaml`:

```yaml
mode: strict_real   # or strict | moderate | aggressive
```

Or from the CLI (does not conflict with `--mode synthetic|historical`):

```bash
python -m backtest --filter-mode strict_real --fast
python -m backtest --filter-mode strict_real --n-markets 5000 --seed 42
```

| Mode | Use when | Target |
|------|----------|--------|
| **strict** | Legacy max-WR profile (inflated-q era) | Fewest trades, WR ~90%+ on old synthetic |
| **strict_real** | Live paper / real `cex_implied_up` as q | High WR; edge 0.14 cuts weak mid-edge buckets |
| **moderate** | More tickets with looser real-q gates | Higher fill rate; VPS showed ~58% WR under real q |
| **aggressive** | Frequency | Highest fills, WR ~80–83% on synthetic |

Presets live in `models/config.py` (`MODE_PRESETS`). Changing `mode:` alone is enough — preset values overwrite the threshold fields on load.

---

## How the Math Delivers 80%+ Win Rate

1. **Misprice** finds markets where the model probability `q` disagrees with the Polymarket price `p`.
2. **Beta conviction** asks: “Given a Beta prior centered on `q`, how sure are we that the true probability is on our side of `p`?”
   - YES bet: `conviction = 1 − BetaCDF(p; α = q·n_eff, β = (1−q)·n_eff)`
3. **Hard filters** only allow a trade when:
   - `|q − p|` is large enough (`min_edge`)
   - conviction is high (`min_conviction`)
   - `q` is extreme (near 0 or 1) — mid-odds coin-flips are skipped
4. **Kelly** sizes the bet fractionally (`κ ≈ 0.35`) and never above 10% of bankroll.
5. **Risk budget** blocks new bets when too much correlated risk is already open.

Net effect: the bot takes **fewer** trades, but those trades are heavily tilted toward wins. Selectivity creates the 80%+ win rate — not magic.

---

## Tuning Guide — Push Win Rate from 80% → 84%+

Edit `config/enhanced_misprice.yaml` (or run `--optimize`):

1. Raise `min_conviction`: `0.95` → `0.97`
2. Raise `min_edge`: `0.12` → `0.14`
3. Push extremes: `extreme_q_high: 0.88`, `extreme_q_low: 0.12`
4. Strengthen Beta: `n_eff.crypto: 100` (was 80)
5. Shrink size while learning: `kappa_base: 0.25`
6. Re-run: `python -m backtest --fast` then `python -m backtest --n-markets 5000`

Or let the searcher do it:

```bash
python -m backtest --optimize --n-markets 5000
```

---

## Interpreting Plots

Saved when plots are enabled (full runs by default; use `--plots` with `--fast` to force):

| File | What to look for |
|------|------------------|
| `equity_drawdown.png` | Equity trending up; red underwater DD shallow and short |
| `calibration.png` | Points near the diagonal → model `q` matches reality |
| `threshold_sweep.png` | Which `min_conviction` lands on 80% / 82% / 85% WR |
| `wr_hist.png` | Monte Carlo WR bump above 80%; 5th percentile not collapsing |

If calibration bows away from the diagonal, **fix the probability model** before trusting a high win rate.

---

## Troubleshooting — If Win Rate Is Below 80%

1. **Check Brier** in the report. If ≥ 0.18, the model is too noisy — 80% isn’t honest until calibration improves.
2. Run optimize: `python -m backtest --optimize --fast`
3. Manually tighten filters (see Tuning Guide above) and re-run `--fast`.
4. Confirm you’re using the enhanced path (default). Add `--compare-baseline` — enhanced should beat naive by a large WR lift.
5. Increase sample size: `--n-markets 8000` so variance doesn’t fake a miss.
6. Read `artifacts/backtest_runs/*/report.txt` — it includes the exact command to reproduce.

Still stuck? Open `report.json` and check `wr_by_edge` — if the `0.06–0.10` bucket is weak, raise `min_edge`.

---

## Artifact layout

Every run creates:

```text
artifacts/backtest_runs/YYYYMMDD_HHMMSS/
  report.txt              # human readable + reproduce command
  report.json             # machine readable
  metrics.json            # alias of key metrics
  parameters_used.yaml    # exact thresholds for this run
  equity_drawdown.png     # (if plots on)
  calibration.png
  threshold_sweep.png
  …
```

---

## Same code as paper trading

The backtester imports the live modules — no duplicated math:

- `strategy.enhanced_misprice.evaluate_market`
- `strategy.kelly` / `strategy.bayesian`
- `risk.portfolio_risk.PortfolioRiskManager`
- `paper_trader.simulator.PaperSimulator`

What you validate here is what the overnight Hermes paper bot uses.
