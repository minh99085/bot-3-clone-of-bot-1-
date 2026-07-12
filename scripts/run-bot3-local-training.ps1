# Bot 3 - one-shot local paper training via Docker Desktop.
# Run from repo root:  .\scripts\run-bot3-local-training.ps1
$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
$Plugin = Join-Path $Root "hermes-agent-main\plugins\hermes-trading-engine"
$Project = "bot3-local"
$DashboardPort = 8810
$Compose = @("-p", $Project, "-f", "docker-compose.yml", "-f", "docker-compose.local.yml")

function Invoke-Compose([string[]]$Args) {
    & docker compose @Compose @Args
    return $LASTEXITCODE
}

Set-Location $Root
Write-Host "==> Preparing .env (Bot 3 local training)..."
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { $py = Get-Command python3 -ErrorAction SilentlyContinue }
if (-not $py) {
    Write-Host "ERROR: Python not found. Install Python 3.12+ or run from a shell where 'python' works." -ForegroundColor Red
    exit 1
}
& $py.Source "$Root\scripts\setup-local-training-env.py"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Set-Location $Plugin

Write-Host "==> Stopping old Bot 3 containers..."
Invoke-Compose @("down", "--remove-orphans") | Out-Null

Write-Host "==> Building images (first run can take 10-15 min)..."
$buildCode = Invoke-Compose @("build")
if ($buildCode -ne 0) {
    Write-Host "BUILD FAILED (exit $buildCode). See errors above." -ForegroundColor Red
    exit $buildCode
}

Write-Host "==> Starting bot3-hermes-training + bot3-hermes-trading-engine..."
$upCode = Invoke-Compose @("up", "-d", "--force-recreate", "--remove-orphans")
if ($upCode -ne 0) {
    Write-Host "START FAILED (exit $upCode). Common causes:" -ForegroundColor Red
    Write-Host "  - Port 8810 or 18787 already in use (stop other bots or change ports in docker-compose.local.yml)"
    Write-Host "  - Invalid .env line (re-run setup-local-training-env.py after git pull)"
    Write-Host "Retrying in foreground to show the exact error..."
    Invoke-Compose @("up", "--force-recreate", "--remove-orphans")
    exit $upCode
}

Invoke-Compose @("ps") | Out-Null

$dashUrl = "http://127.0.0.1:$DashboardPort/dashboard"
$healthUrl = "http://127.0.0.1:$DashboardPort/api/health"

Write-Host ""
Write-Host "Bot 3 local training started (project: $Project)."
Write-Host "  Dashboard : $dashUrl"
Write-Host "  Health    : $healthUrl"
Write-Host "  Diagnose  : .\scripts\diagnose-bot3-local.ps1"
Write-Host "  Logs      : docker compose -p $Project -f docker-compose.yml -f docker-compose.local.yml logs -f hermes-training"
Write-Host ""

$ok = $false
foreach ($wait in @(5, 10, 15, 20, 30)) {
    Start-Sleep -Seconds $wait
    try {
        $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 10
        Write-Host "Health check OK (${wait}s): $($health | ConvertTo-Json -Compress)"
        $ok = $true
        break
    } catch {
        Write-Host "Waiting for dashboard API... (${wait}s)"
    }
}

if ($ok) {
    Write-Host "Opening dashboard in default browser..."
    Start-Process $dashUrl
} else {
    Write-Host "Dashboard API not ready yet. Run: .\scripts\diagnose-bot3-local.ps1" -ForegroundColor Yellow
}
