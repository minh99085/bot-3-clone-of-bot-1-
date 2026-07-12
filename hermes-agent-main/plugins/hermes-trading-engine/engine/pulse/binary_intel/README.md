# Binary Intel — invented bot capability (Bot 3)

Quantitative + Grok scripts that make the bot smarter on Polymarket BTC/ETH
binary markets using **5m RSI Divergence** alerts on INDEX charts for **all lanes**.

## What was invented

| Module | Role |
|--------|------|
| `math_core.py` | Digital-option math: displacement \(z\), \(d_2\), binary θ, Shannon entropy, RSI info gain, estimation-error Kelly, convergence edge |
| `tv_universal.py` | Same INDEX 5m RSI alerts feed **5m / 15m / 1h** and **BTC + ETH** with cross-asset agreement |
| `pre_trade.py` | Pre-fill script → intelligence score, size mult, hard-block, Grok brief |
| `post_trade.py` | Settlement learner → Brier grade, RSI alignment WR, weight self-tune, LessonsBook rules |
| `grok_protocol.py` | Structured pre/post Grok compute payloads (uses existing GrokDecider budget/shadow) |

## Formulas (core)

\[
z = \frac{\ln(S_{\text{now}}/S_{\text{open}})}{\sigma\sqrt{t}},\quad
P(\text{Up})=\Phi(d_2),\quad
\theta=\varphi(d_2)\frac{\partial d_2}{\partial t}
\]

\[
H(p)=-p\log_2 p-(1-p)\log_2(1-p),\quad
\text{IG}=H(\text{prior})-H(\text{posterior}),\quad
\text{posterior odds}=\text{prior odds}\times LR_{\text{RSI}}
\]

\[
f^*=\frac{p-c}{1-c},\quad
f_{\text{adj}}=\text{fraction}\cdot f^*\cdot\max(0,1-2\sigma_p)
\]

## Wiring

- **Pre-trade:** `Engine._run_pre_trade_analysis` + Osmani `_osmani_execute_verified`
- **Post-trade:** `Engine._settle_due` → grades + lessons; `lane_15m_learner` now receives `tv_rsi_overlay_aligned`
- **Grok bundle:** `binary_intel` + `binary_intel_learner` keys
- **Env:** `PULSE_BINARY_INTEL_ENABLED=1` (default on)

## Invariants

- Restrict-only (size down / hard-block weak); never bypasses `evaluate_execution`
- Chainlink `price_action_trend` remains primary side signal; RSI is confirm/fade
- Grok stays shadow-gradable via existing decider path
