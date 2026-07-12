#!/usr/bin/env bash
# One babysit cycle: health → pull report → evaluate → optional WR tune → push state.
# PAPER ONLY. Does not enable live trading.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
BABYSIT="$REPO_ROOT/scripts/pulse-babysit"
VPS_URL="${BOT1_VPS_URL:-http://144.202.122.120}"
APPLY_WR_TUNE="${APPLY_WR_TUNE:-1}"
SKIP_PUSH="${SKIP_PUSH:-0}"

cd "$REPO_ROOT"

echo "=== scan-health ==="
python3 "$BABYSIT/scan-health.py" "$VPS_URL" | tee /tmp/scan-health.json || true

echo "=== validate-frozen-lock ==="
python3 "$BABYSIT/validate-frozen-lock.py" || true

echo "=== pull VPS artifacts ==="
SKIP_PUSH=1 bash "$BABYSIT/pull-vps-artifacts.sh"

echo "=== evaluate-cycle ==="
EVAL_JSON="$(python3 "$BABYSIT/evaluate-cycle.py")"
echo "$EVAL_JSON" | tee /tmp/evaluate-cycle.json
VERDICT="$(echo "$EVAL_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin).get('verdict','?'))")"

WR_APPLIED=0
if [[ "$APPLY_WR_TUNE" == "1" ]]; then
  STARVATION="$(echo "$EVAL_JSON" | python3 -c "
import json,sys
d=json.load(sys.stdin)
codes={i.get('code') for i in d.get('issues',[])}
print('1' if codes & {'trade_starvation','trade_starvation_streak'} else '0')
")"
  if [[ "$STARVATION" == "0" ]]; then
    if echo "$EVAL_JSON" | python3 -c "
import json,sys
d=json.load(sys.stdin)
codes={i.get('code') for i in d.get('issues',[])}
band=bool(d.get('band_issues'))
want={'win_rate_below_target','cheap_down_bleed','expensive_down_bleed','sweet_spot_underuse'}
import sys
sys.exit(0 if (band or codes & want) else 1)
"; then
      echo "=== apply-wr-tune ==="
      if python3 "$BABYSIT/apply-wr-tune.py" --eval-json "$EVAL_JSON" --apply; then
        WR_APPLIED=1
      fi
    else
      echo "Skipping WR tune — no band/WR issues"
    fi
  else
    echo "Skipping WR tune — trade starvation active"
  fi
fi

# Bump state.json cycle metadata
python3 - <<'PY'
import json
from datetime import datetime, timezone
from pathlib import Path
p = Path("scripts/pulse-babysit/state.json")
st = json.loads(p.read_text(encoding="utf-8"))
st["babysit_autopilot"] = True
st["phase"] = "continuous"
st["cycle"] = int(st.get("cycle") or 0) + 1
st["last_eval_at"] = datetime.now(timezone.utc).isoformat()
try:
    ev = json.loads(Path("/tmp/evaluate-cycle.json").read_text())
    st["last_verdict"] = ev.get("verdict")
    hist = list(st.get("history") or [])
    hist.append({
        "ts": st["last_eval_at"],
        "verdict": ev.get("verdict"),
        "metrics": ev.get("metrics") or {},
        "issue_codes": [i.get("code") for i in (ev.get("issues") or [])],
    })
    st["history"] = hist[-24:]
except Exception:
    pass
p.write_text(json.dumps(st, indent=2) + "\n", encoding="utf-8")
print("Updated state.json cycle", st["cycle"])
PY

if [[ "$SKIP_PUSH" != "1" ]]; then
  git add scripts/pulse-babysit/state.json vps_full_reports/latest/ monitoring/ 2>/dev/null || true
  if [[ "$WR_APPLIED" == "1" ]]; then
    git add scripts/apply-loop-arch-env.py scripts/pulse-babysit/frozen-env-keys.json 2>/dev/null || true
  fi
  if ! git diff --cached --quiet; then
    git commit -m "chore(babysit): cycle verdict=${VERDICT} wr_tune=${WR_APPLIED} [skip ci]" || true
    git push origin main || true
  fi
  if [[ "$WR_APPLIED" == "1" ]]; then
    echo "=== VPS deploy (env changed) ==="
  fi
fi

echo "BABYSIT_CYCLE_DONE verdict=$VERDICT wr_tune=$WR_APPLIED"
