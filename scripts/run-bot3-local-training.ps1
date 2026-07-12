# Bot 3 — one-shot local paper training via Docker Desktop.
# Run from repo root:  .\scripts\run-bot3-local-training.ps1
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Plugin = Join-Path $Root "hermes-agent-main\plugins\hermes-trading-engine"

Set-Location $Root
Write-Host "==> Preparing .env (Bot 3 local training)..."
python "$Root\scripts\setup-local-training-env.py"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Set-Location $Plugin
$Compose = @("-f", "docker-compose.yml", "-f", "docker-compose.local.yml")

Write-Host "==> Stopping old containers..."
docker compose @Compose down --remove-orphans

Write-Host "==> Building images (RUN_TESTS=0 for local)..."
docker compose @Compose build
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "==> Starting hermes-training + hermes-trading-engine..."
docker compose @Compose up -d --force-recreate --remove-orphans
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host ""
Write-Host "Bot 3 local training is up."
Write-Host "  Dashboard : http://localhost:8800/dashboard"
Write-Host "  Health    : http://localhost:8800/api/health"
Write-Host "  Logs      : docker compose -f docker-compose.yml -f docker-compose.local.yml logs -f hermes-training"
Write-Host ""
Start-Sleep -Seconds 8
try {
    $health = Invoke-RestMethod -Uri "http://localhost:8800/api/health" -TimeoutSec 15
    Write-Host "Health check: $($health | ConvertTo-Json -Compress)"
} catch {
    Write-Host "Health check pending — training loop may still be warming up. Check logs above."
}
