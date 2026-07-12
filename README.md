# Bot-1

**Bot 1** — standalone BTC pulse paper bot (Hermes trading engine).

| | Bot 1 |
|---|-------|
| **Strategy** | `arb_first_perfect_wr_v1` (sweet-spot 0.47–0.55 entry band) |
| **VPS** | `144.202.122.120` (`ssh bot1` / `root@144.202.122.120`) |
| **Dashboard** | http://144.202.122.120/ |
| **Path** | `/opt/Bot-1` |
| **Deploy** | `.\scripts\sync-vps.ps1` |

## Quick start (operator)

```powershell
cd C:\Users\tieut\Bot-1
.\scripts\sync-vps.ps1
```

**Bot 3 local training (Docker Desktop):**

```powershell
.\scripts\run-bot3-local-training.ps1
```

Dashboard: http://localhost:8810/dashboard

Profile: `scripts/bot-profile.json`

**Loop runtime:** paper loop runs 24×7 on VPS (`hermes-training`). GitHub Actions
`.github/workflows/bot-1_loop.yml` is a **15m health monitor only** (no trading) — see
`docs/cloud-vs-vps-loop.md`.