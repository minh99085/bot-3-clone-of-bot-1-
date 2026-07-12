# Bot 3 — project rules

## Cursor Cloud (`bot-3-clone-of-bot-1-`)

Flat copy of Bot-1 logic in `/workspace` for **Bot 3 Directional** paper training.

**Canonical GitHub:** `https://github.com/minh99085/bot-3-clone-of-bot-1-`

---

## Operator deploy rules (ALWAYS follow — Bot 3)

Read **`.grok/rules/bot3-deploy-policy.md`** every session.

1. **Always push to `main`** — commit directly on `main`. **Do not create feature branches or PRs.**
2. **Always push to VPS** after every `git push origin main`.
3. **Always remove orphans and rebuild** after VPS sync:
   `docker compose down --remove-orphans` → `build` → `up -d --force-recreate --remove-orphans`.

**Ship pipeline (always, in order):** commit on `main` → `git push origin main` →
`.\scripts\sync-vps-bot3.ps1` (or `./scripts/sync-vps-bot3.sh`) → verify VPS HEAD == `origin/main`.

Job is **incomplete** until VPS rebuild finishes. Never `-SkipRebuild` unless operator explicitly
requests code-only sync in the current message.

## Cloud agent SSH access (required for autonomous VPS deploy)

This cloud VM cannot reach the VPS until **one** of these is done:

### Option A — Cursor secret (fastest; uses your laptop key)

1. Open [Cloud Agents → Environments](https://cursor.com/dashboard/cloud-agents/environments/r/github.com/minh99085/bot-3-clone-of-bot-1-)
2. **Secrets** tab → add **Runtime Secret** `BOT3_VPS_SSH_PRIVATE_KEY`
3. Value = full contents of `%USERPROFILE%\.ssh\hermes-laptop-vps` (private key PEM)
4. **Update environment** / start a new cloud agent run

`scripts/materialize-vps-ssh-key.sh` writes the secret to `~/.ssh/hermes-laptop-vps` on boot.

### Option B — Grant cloud-agent key (one-time from laptop)

Run **once** on Windows (no git pull needed — paste as-is):

```powershell
ssh -i "$env:USERPROFILE\.ssh\hermes-laptop-vps" root@207.246.96.45 "grep -qF 'bot3-cloud-agent' ~/.ssh/authorized_keys || echo 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFYJ352J+SrH4CZsOfGds87X4B9lig4ci+PHOgEuBIjK bot3-cloud-agent' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
```

Or after `git pull`: `.\scripts\grant-cloud-agent-ssh.ps1`

Public key file: `scripts/keys/bot3-cloud-agent.pub`

| Item | Value |
|------|-------|
| VPS | `root@207.246.96.45` |
| Path | `/opt/Bot-3` |
| Dashboard | http://207.246.96.45/dashboard (`Bot 3 Directional`) |
| TradingView | http://207.246.96.45/webhooks/tradingview |
| Local laptop | `C:\hermes-agent\bot-3-clone-of-bot-1-` |

---

## Local laptop training (Docker Desktop)

```powershell
.\scripts\run-bot3-local-training.ps1
```

Dashboard: http://localhost:8810/dashboard (local profile; TV gates OFF).

---

## Install / test (VM baseline: Python 3.12, Node.js v22)

```bash
cd hermes-agent-main/plugins/hermes-trading-engine
pip install -r requirements.txt -r requirements-dev.txt
python3 -m pytest
```

---

## Retained invariants (never lift)

- **PAPER ONLY** — no live trading without explicit operator override in the current message.
- **Honest accounting** — no inflated performance.
- **Loop Engineering architecture lock** — see `.grok/rules/loop-engineering-lock.md`.
- Run `validate-frozen-lock.py` before VPS deploy.

Operator guide: `hermes-agent-main/plugins/hermes-trading-engine/AGENTS.md`
