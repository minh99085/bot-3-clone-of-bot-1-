#!/usr/bin/env bash
# Commit and push vps_full_reports/latest/ to origin/main (Linux/cloud).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
LATEST="$REPO_ROOT/vps_full_reports/latest"
SKIP_PUSH="${SKIP_PUSH:-0}"

REQUIRED=(FULL_REPORT.md report.md report.docx btc_pulse_status.json btc_pulse_ledger.json btc_pulse_light_report.json)
for f in "${REQUIRED[@]}"; do
  [[ -f "$LATEST/$f" ]] || { echo "Cannot push: missing $f" >&2; exit 1; }
done

cd "$REPO_ROOT"
settled="?"
wr="?"
pf="?"
grade="?"
if command -v python3 >/dev/null; then
  read -r settled wr pf <<< "$(python3 - <<'PY'
import json
from pathlib import Path
p = Path("vps_full_reports/latest/btc_pulse_status.json")
d = json.loads(p.read_text(encoding="utf-8-sig"))
led = d.get("ledger") or {}
print(led.get("settled","?"), led.get("win_rate","?"), led.get("profit_factor","?"))
PY
)"
fi
if [[ -f monitoring/technical-grades.json ]]; then
  grade="$(python3 -c "import json; print(json.load(open('monitoring/technical-grades.json')).get('composite',{}).get('grade','?'))" 2>/dev/null || echo "?")"
fi

while IFS= read -r rel; do
  [[ -n "$rel" ]] || continue
  if [[ ! -f "$REPO_ROOT/$rel" ]]; then
    git rm -f -- "$rel" 2>/dev/null || true
    echo "Removed stale tracked file: $rel"
  fi
done < <(git ls-files "vps_full_reports/latest/" 2>/dev/null || true)

git add -f vps_full_reports/latest/
git add -f monitoring/timeline.jsonl monitoring/latest-snapshot.json 2>/dev/null || true
git add -f monitoring/technical-grades.json monitoring/grades-history.jsonl monitoring/TECHNICAL_GRADES.md monitoring/TECHNICAL_REPORT.md 2>/dev/null || true

if git diff --cached --quiet; then
  echo "Report unchanged — nothing to commit"
  exit 0
fi

ts="$(date -u +'%Y-%m-%d %H:%M UTC')"
msg="chore(reports): VPS full report ${ts} (${settled} settled, WR ${wr}, PF ${pf}, grade ${grade})"
git commit -m "$msg"
if [[ "$SKIP_PUSH" == "1" ]]; then
  echo "Committed locally (SKIP_PUSH): $msg"
else
  git push origin main
  echo "Pushed report to origin/main: $msg"
fi
