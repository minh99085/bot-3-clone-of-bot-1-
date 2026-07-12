# AGENTS.md

## Cursor Cloud specific instructions

This repository (`bot-3-clone-of-bot-1-`) is intended to hold a flat copy of **Bot-1** on `main` — no nested clone folder. The source must be `https://github.com/minh99085/Bot-1` only. **Do not use `Arb-bot` or other repos as a substitute.**

### Current state

As of setup, `main` contains only `README.md`. `minh99085/Bot-1` returns 404 / "Repository not found" from this environment, so application dependencies, lint, tests, and run commands are unknown until that repo is published and cloned into the workspace root.

### When Bot-1 becomes available

1. Clone to a temp path, then copy contents into `/workspace` without nesting:

```bash
git clone https://github.com/minh99085/Bot-1 /tmp/bot-1
tar -C /tmp/bot-1 --exclude='.git' -cf - . | tar -C /workspace -xf -
```

2. Inspect the repo for its dependency manifests (`package.json`, `pyproject.toml`, `requirements.txt`, etc.) and follow its README for install/run.
3. Set the VM update script to match whatever dependency install command the Bot-1 repo documents (for example `npm install`, `uv sync`, or `pip install -r requirements.txt`).

### VM baseline (already present)

- Python 3.12
- Node.js v22

No project-specific services are running until Bot-1 code is present.
