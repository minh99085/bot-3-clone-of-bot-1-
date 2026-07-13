#!/usr/bin/env bash
# Package training data into a single tarball for easy copy to Samsung T7.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SRC="${1:-$ROOT/data/polymarket-training}"
OUT="${2:-$ROOT/data/polymarket-training-bundle.tar.gz}"
if [[ ! -d "$SRC" ]]; then
  echo "Missing $SRC — run download_crypto_windows.py first" >&2
  exit 1
fi
tar -C "$(dirname "$SRC")" -czf "$OUT" "$(basename "$SRC")"
ls -lh "$OUT"
echo "Copy this file to your T7: $OUT"
