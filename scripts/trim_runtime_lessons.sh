#!/usr/bin/env bash
# Trim runtime lessons (ALLOCATION_REJECT / session AVOID blocks) without wiping ledgers.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
python3 << 'PY'
import re
from pathlib import Path

path = Path("knowledge/LESSONS.md")
if not path.is_file():
    raise SystemExit("no LESSONS.md")
text = path.read_text()
marker = "<!-- lessons_engine appends new dated lessons below -->"
before = len(text)
text = re.sub(
    r"\n### \[.*?ALLOCATION_REJECT.*?(?=\n### |\Z)",
    "",
    text,
    flags=re.DOTALL,
)
text = re.sub(
    r"\n### \[.*?AVOID:mispricing.*?(?=\n### |\Z)",
    "",
    text,
    flags=re.DOTALL,
)
if marker in text:
    text = text.split(marker)[0] + marker + "\n"
path.write_text(text)
print(f"trimmed LESSONS.md ({before} -> {len(text)} bytes)")
PY
echo "Done. Restart bots if they cache lessons in-process."
