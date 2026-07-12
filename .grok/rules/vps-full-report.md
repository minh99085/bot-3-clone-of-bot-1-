# VPS full report — mandatory publish workflow (operator rule)

**Non-negotiable:** The live bot MUST generate a **real, complete** full report on every tick.
Published snapshots go **only** to:

`https://github.com/minh99085/Bot-1/tree/main/vps_full_reports/latest`

Do not publish reports elsewhere on `main` (no dated subfolders, no duplicate copies).

## Engine (VPS) — generate real full report

The pulse engine (`engine/pulse/engine.py` `_persist`) must write the full provenance bundle to
`/data` inside `hermes-training`:

| File | Purpose |
|------|---------|
| `FULL_REPORT.md` | **Primary** human-readable report (dep-arb, P-UP, calibration, Kelly, trades, oracle, P&L) |
| `report.md` | Short summary |
| `report.docx` | Word export |
| `LESSONS.md` | Operator lessons |
| `STATE.md` | Engine state snapshot |
| `MANIFEST.txt` | Artifact manifest |
| `validation_full.txt` / `validation_light.txt` | Validation output |
| `btc_pulse_meta_bundle.json` | Meta bundle |

If `FULL_REPORT.md` is missing on VPS after deploy, treat as **P0** — fix reporting code, redeploy,
then re-pull.

## Pull + publish (local agent)

1. **Wipe** `vps_full_reports/latest/` locally before every pull (no stale files).
2. Run `.\scripts\pulse-babysit\pull-vps-artifacts.ps1` — pulls live VPS artifacts + API JSON.
3. **Require** `FULL_REPORT.md` in the pulled snapshot; fail the pull if absent.
4. Run `.\scripts\pulse-babysit\push-report-to-main.ps1` — **remove all old tracked files** in
   `vps_full_reports/latest/` from git, then commit **only** the fresh snapshot and push to
   `origin/main`.

Use `-SkipPush` only for local debugging.

## Never

- Commit a partial report (missing `FULL_REPORT.md` or required JSON).
- Leave orphan/stale files in `vps_full_reports/latest/` on `main`.
- Publish full reports to paths outside `vps_full_reports/latest/`.
- Skip wipe-before-pull or wipe-before-push when refreshing reports.

## Canonical URL (share with operator)

https://github.com/minh99085/Bot-1/tree/main/vps_full_reports/latest