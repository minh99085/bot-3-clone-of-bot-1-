#!/bin/bash
set -e
echo "=== ENGINE ENV (phase B-F keys) ==="
docker exec hermes-trading-engine env | grep -E 'PULSE_(DEPENDENCY|BREGMAN|RESEARCH|ETH|ARB_NONATOMIC|PRIMARY|SIZING)' | sort

echo ""
echo "=== ENGINE ERRORS (30m) ==="
docker logs hermes-trading-engine --since 30m 2>&1 | grep -iE 'error|exception|traceback|fail' | tail -20 || echo "(none)"

echo ""
echo "=== TRAINING ERRORS (30m) ==="
docker logs hermes-training --since 30m 2>&1 | grep -iE 'error|exception|traceback' | tail -10 || echo "(none)"

echo ""
echo "=== PERSISTED DATA ==="
docker exec hermes-training ls -la /data/btc_pulse*.json 2>/dev/null | tail -10

echo ""
echo "=== TV PERSISTED STATE ==="
docker exec hermes-training python3 -c "
import json
from pathlib import Path
p = Path('/data/btc_pulse_tradingview.json')
if p.exists():
    d = json.loads(p.read_text())
    print('reject_reasons:', d.get('reject_reasons'))
    lbt = d.get('latest_by_tf') or {}
    if isinstance(lbt, dict):
        for k,v in sorted(lbt.items()):
            print(' ', k, '->', v)
    else:
        print('latest_by_tf:', lbt[:8] if lbt else [])
else:
    print('missing btc_pulse_tradingview.json')
"