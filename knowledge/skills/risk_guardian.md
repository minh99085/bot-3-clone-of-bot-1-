# Risk-Guardian Meta-Controller (RGMC)

## Mandate
Watch rolling WR, drawdown, concentration. **Tighten only.** Never loosen frozen gates.

## Actions
| Condition | Action |
|-----------|--------|
| WR(rolling) < 78% (n≥15) | ↓ `soft_kappa_scale`, ↓ `size_multiplier` |
| DD ≥ 8% | Cap soft κ ≤0.55, size× ≤0.50 |
| WR < 78% (n≥25) after promote | **ROLLBACK** registry prod → prior |
| WR ≥ 85% & DD < 4% | Soft-recover scales toward 1.0 (still ≤1) |
| Weak MCHB family | Disable family arm → skip |

## Audit
- `data/paper/<instance>/rgmc_audit.jsonl`
- `knowledge/LESSONS.md` → `## Risk-Guardian Audit`
- `STATE.md` fields: Autonomy WR / DD / Soft κ / Size×

## Alerts
Telegram (`TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`) and Slack (`SLACK_WEBHOOK_URL`) on promote / rollback / DD only.

## Auto Log
