"""Git worktree isolation for parallel research / signal / risk lanes.

Two agents writing the same file is the same headache as two engineers
committing the same lines — isolate at handoff time.

Worktree creation is best-effort: if git worktrees are unavailable (shallow
clone, permissions), we fall back to a plain directory under `.worktrees/`
so the trading loop still runs in paper mode.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
WORKTREE_ROOT = ROOT / ".worktrees"


@dataclass
class WorktreeHandle:
    name: str
    path: Path
    branch: str
    is_git_worktree: bool = True

    def run(self, *cmd: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(cmd),
            cwd=self.path,
            check=check,
            text=True,
            capture_output=True,
        )


def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=check,
        text=True,
        capture_output=True,
    )


def _fallback_dir(name: str, branch: str) -> WorktreeHandle:
    path = WORKTREE_ROOT / name
    path.mkdir(parents=True, exist_ok=True)
    marker = path / ".hermes_lane"
    if not marker.exists():
        marker.write_text(f"lane={name}\nbranch={branch}\n", encoding="utf-8")
    logger.warning("worktree fallback (plain dir): %s -> %s", name, path)
    return WorktreeHandle(name=name, path=path, branch=branch, is_git_worktree=False)


def ensure_worktree(name: str, branch: Optional[str] = None) -> WorktreeHandle:
    """Create or reuse an isolated worktree for a lane.

    Lanes used by Hermes:
      - research   (backtesting / alpha research)
      - signal     (live signal gen + verification)
      - risk       (risk monitor — never blocks execution path)
    """
    WORKTREE_ROOT.mkdir(parents=True, exist_ok=True)
    path = WORKTREE_ROOT / name
    branch = branch or f"hermes/{name}"

    if path.exists() and (path / ".git").exists():
        return WorktreeHandle(name=name, path=path, branch=branch)
    if path.exists() and (path / ".hermes_lane").exists():
        return WorktreeHandle(name=name, path=path, branch=branch, is_git_worktree=False)

    try:
        # Prune stale registrations after manual rm -rf .worktrees
        _git("worktree", "prune", check=False)
    except FileNotFoundError:
        return _fallback_dir(name, branch)

    try:
        existing = _git("branch", "--list", branch, check=False)
        if path.exists() and not (path / ".git").exists():
            # Leftover plain dir — reuse as fallback
            return _fallback_dir(name, branch)
        if existing.stdout.strip():
            result = _git("worktree", "add", str(path), branch, check=False)
        else:
            result = _git("worktree", "add", "-b", branch, str(path), check=False)
        if result.returncode != 0:
            logger.warning(
                "git worktree add failed (%s): %s",
                result.returncode,
                (result.stderr or result.stdout or "").strip(),
            )
            return _fallback_dir(name, branch)
        logger.info("worktree ready: %s -> %s", name, path)
        return WorktreeHandle(name=name, path=path, branch=branch)
    except FileNotFoundError:
        return _fallback_dir(name, branch)
    except Exception as exc:  # noqa: BLE001
        logger.warning("worktree unavailable (%s); using fallback dir", exc)
        return _fallback_dir(name, branch)


def remove_worktree(name: str, *, force: bool = False) -> None:
    path = WORKTREE_ROOT / name
    if not path.exists():
        return
    args = ["worktree", "remove", str(path)]
    if force:
        args.append("--force")
    result = _git(*args, check=False)
    if result.returncode != 0 and path.exists():
        # Fallback cleanup
        import shutil

        shutil.rmtree(path, ignore_errors=True)
    _git("worktree", "prune", check=False)
    logger.info("worktree removed: %s", name)


def list_worktrees() -> list[str]:
    result = _git("worktree", "list", "--porcelain", check=False)
    paths = []
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            paths.append(line.split(" ", 1)[1])
    return paths
