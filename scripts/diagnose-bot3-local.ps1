# Bot 3 - diagnose local Docker training + dashboard.
# Run from repo root:  .\scripts\diagnose-bot3-local.ps1
$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
$Plugin = Join-Path $Root "hermes-agent-main\plugins\hermes-trading-engine"
$Project = "bot3-local"
$Compose = @("-p", $Project, "-f", "docker-compose.yml", "-f", "docker-compose.local.yml")

function Section($title) {
    Write-Host ""
    Write-Host "=== $title ===" -ForegroundColor Cyan
}

$DashboardPort = 8810

Set-Location $Plugin

Section "Docker Desktop"
try {
    docker version --format 'Docker {{.Server.Version}}' 2>$null
} catch {
    Write-Host 'FAIL: Docker not reachable. Start Docker Desktop and wait until it says Running.' -ForegroundColor Red
}

Section "Bot 3 containers"
docker compose @Compose ps -a

Section "Port 8810 (Bot 3 dashboard)"
$port = Get-NetTCPConnection -LocalPort $DashboardPort -ErrorAction SilentlyContinue | Select-Object -First 1
if ($port) {
    Write-Host "OK: something is listening on port $DashboardPort (state=$($port.State))"
} else {
    Write-Host "WARN: nothing listening on port $DashboardPort - API container may not be up." -ForegroundColor Yellow
}

Section "Health API"
try {
    $h = Invoke-RestMethod -Uri "http://127.0.0.1:$DashboardPort/api/health" -TimeoutSec 5
    Write-Host ($h | ConvertTo-Json -Depth 4)
    if (-not $h.pulse_status_fresh) {
        Write-Host 'NOTE: pulse_status_fresh=false is normal for the first 1-2 minutes after start.' -ForegroundColor Yellow
    }
} catch {
    Write-Host "FAIL: cannot reach http://127.0.0.1:$DashboardPort/api/health" -ForegroundColor Red
    Write-Host $_.Exception.Message
}

Section "Pulse status"
try {
    $p = Invoke-RestMethod -Uri "http://127.0.0.1:$DashboardPort/api/polymarket/training/btc_pulse" -TimeoutSec 5
    if ($p.available) {
        Write-Host "OK: training loop has written status (ticks=$($p.ticks))"
    } else {
        Write-Host "WAIT: $($p.reason)" -ForegroundColor Yellow
    }
} catch {
    Write-Host 'SKIP: pulse status not available yet.'
}

Section "Last 25 lines - bot3-hermes-trading-engine (dashboard/API)"
docker logs --tail 25 bot3-hermes-trading-engine 2>&1

Section "Last 25 lines - bot3-hermes-training (training loop)"
docker logs --tail 25 bot3-hermes-training 2>&1

Section "Quick fixes"
Write-Host '1. Restart everything:'
Write-Host '     .\scripts\run-bot3-local-training.ps1'
Write-Host '2. Dashboard URL (try both):'
Write-Host "     http://127.0.0.1:$DashboardPort/dashboard"
Write-Host "     http://localhost:$DashboardPort/dashboard"
Write-Host '3. If port 8810 is taken, edit hermes-agent-main\plugins\hermes-trading-engine\docker-compose.local.yml'
Write-Host '     change 8810:8800 to another free host port (e.g. 8811:8800)'
Write-Host '4. Live logs:'
Write-Host '     cd hermes-agent-main\plugins\hermes-trading-engine'
Write-Host '     docker compose -p bot3-local -f docker-compose.yml -f docker-compose.local.yml logs -f hermes-training'
