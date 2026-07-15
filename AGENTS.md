# AGENTS.md

## Cursor Cloud specific instructions

**Financial Freedom Bot (Hermes v2)** — autonomous Polymarket trading loop.

### VPS baseline

- **Host:** `207.246.96.45` (user `root`)
- **Deploy path:** `/opt/financial-freedom-bot`
- **SSH:** `~/.ssh/bot3_cloud_agent` (or `BOT3_VPS_SSH_PRIVATE_KEY`)
- **Deploy:** `./deploy/deploy_vps.sh`

### VM baseline (cloud agent environment)

- Python 3.12
- Node.js v22
- Docker available on VPS

### Install / run (keep in sync with README)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export PYTHONPATH=.
python -m hermes.hermes_loop once          # paper turn
python -m hermes.hermes_loop overnight     # cadence + risk monitor
pytest -q
```

### Architecture pointers

- Living skills: `knowledge/SKILL.md`, `ALPHA_RESEARCH_SKILL.md`
- Memory: `knowledge/STATE.md`, `LESSONS.md`
- Verifier is sacred: `hermes/verifier.py` — do not weaken gates casually
- Paper default; live requires `HERMES_LIVE=1` + STATE `Live Enabled: true`
