#!/usr/bin/env bash
# Spawn isolated worktrees for parallel strategy discovery.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
git worktree add -B discovery/directional worktrees/directional 2>/dev/null || true
git worktree add -B discovery/final_seconds worktrees/final_seconds 2>/dev/null || true
git worktree add -B discovery/maker worktrees/maker 2>/dev/null || true
git worktree list