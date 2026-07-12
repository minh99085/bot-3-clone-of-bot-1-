# Pull pulse artifacts from VPS (docker volume via hermes-training) into vps_full_reports/latest/
# Always commits + pushes to origin/main by default (includes report.docx).
param(
    [string]$SshKey = "$env:USERPROFILE\.ssh\bot1_grok_temp",
    [string]$VpsHost = "144.202.122.120",
    [string]$VpsUser = "root",
    [string]$Container = "hermes-training",
    [switch]$SkipPush
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$Dest = Join-Path $RepoRoot "vps_full_reports\latest"
if (Test-Path $Dest) {
    Get-ChildItem -Path $Dest -Force | Remove-Item -Recurse -Force
    Write-Host "Cleared stale files in vps_full_reports/latest/"
}
New-Item -ItemType Directory -Force -Path $Dest | Out-Null

$sshArgs = @("-i", $SshKey, "-o", "ConnectTimeout=20", "-o", "StrictHostKeyChecking=no", "${VpsUser}@${VpsHost}")
$remoteDir = "/data"
$files = @(
    "btc_pulse_status.json",
    "btc_pulse_light_report.json",
    "btc_pulse_ledger.json",
    "btc_pulse_tradingview.json",
    "report.md",
    "report.docx",
    "btc_pulse_score_history.json"
)

function Copy-RemoteFile {
    param([string]$RemotePath, [string]$LocalPath, [switch]$Binary)
    if ($Binary) {
        $b64 = ssh @sshArgs "docker exec ${Container} base64 -w0 ${RemotePath} 2>/dev/null || exit 1"
        if (-not $b64) { throw "remote read failed for $RemotePath" }
        [System.IO.File]::WriteAllBytes($LocalPath, [Convert]::FromBase64String($b64))
        return
    }
    $raw = ssh @sshArgs "docker exec ${Container} cat ${RemotePath} 2>/dev/null || exit 1"
    if (-not $raw) { throw "remote read failed for $RemotePath" }
    [System.IO.File]::WriteAllText($LocalPath, $raw, [System.Text.UTF8Encoding]::new($false))
}

function Save-ApiJson {
    param([string]$Url, [string]$LocalPath)
    $resp = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 30
    if ($resp.Content -match '"available"\s*:\s*false') {
        throw "API unavailable: $Url"
    }
    [System.IO.File]::WriteAllText($LocalPath, $resp.Content, [System.Text.UTF8Encoding]::new($false))
}

# Status + ledger: live API (same JSON the dashboard serves; avoids stale docker volume copies on Windows scp).
try {
    Save-ApiJson "http://${VpsHost}/api/polymarket/training/btc_pulse" (Join-Path $Dest "btc_pulse_status.json")
    Write-Host "  ok btc_pulse_status.json (api)"
    Save-ApiJson "http://${VpsHost}/api/polymarket/training/btc_pulse/ledger" (Join-Path $Dest "btc_pulse_ledger.json")
    Write-Host "  ok btc_pulse_ledger.json (api)"
} catch {
    Write-Warning "API pull failed ($($_.Exception.Message)); falling back to docker cp"
    Copy-RemoteFile "$remoteDir/btc_pulse_status.json" (Join-Path $Dest "btc_pulse_status.json")
    Write-Host "  ok btc_pulse_status.json"
    Copy-RemoteFile "$remoteDir/btc_pulse_ledger.json" (Join-Path $Dest "btc_pulse_ledger.json")
    Write-Host "  ok btc_pulse_ledger.json"
}

$volumeRequired = @(
    "FULL_REPORT.md",
    "btc_pulse_light_report.json",
    "btc_pulse_tradingview.json",
    "report.md",
    "report.docx",
    "btc_pulse_score_history.json",
    "btc_pulse_meta_bundle.json",
    "LESSONS.md",
    "STATE.md",
    "MANIFEST.txt",
    "validation_full.txt",
    "validation_light.txt"
)
$volumeOptional = @("REPORT_EPOCH.json")
foreach ($f in $volumeRequired) {
    $isBinary = $f -eq "report.docx"
    Copy-RemoteFile "$remoteDir/$f" (Join-Path $Dest $f) -Binary:$isBinary
    Write-Host "  ok $f"
}
foreach ($f in $volumeOptional) {
    $isBinary = $f -eq "report.docx"
    try {
        Copy-RemoteFile "$remoteDir/$f" (Join-Path $Dest $f) -Binary:$isBinary
        Write-Host "  ok $f"
    } catch {
        Write-Warning "  skip $f (not on VPS yet)"
    }
}

foreach ($f in @("btc_pulse_status.json", "report.docx", "FULL_REPORT.md")) {
    if (-not (Test-Path (Join-Path $Dest $f))) {
        Write-Error "Pull failed: $f missing (engine must generate real full report on VPS)"
    }
}
Write-Host "Pulled artifacts -> $Dest"

$epochScript = Join-Path $PSScriptRoot "apply-report-epoch.py"
if (Test-Path $epochScript) {
    python3 $epochScript
}

$summaryScript = Join-Path $PSScriptRoot "write-cycle-summary.py"
if (Test-Path $summaryScript) {
    python $summaryScript
}

$timelineScript = Join-Path $PSScriptRoot "record-timeline.py"
if (Test-Path $timelineScript) {
    python $timelineScript --from-latest
}

$gradeScript = Join-Path $PSScriptRoot "grade-technical.py"
if (Test-Path $gradeScript) {
    python $gradeScript
}

$pushScript = Join-Path $PSScriptRoot "push-report-to-main.ps1"
if (-not (Test-Path $pushScript)) {
    Write-Error "Missing push script: $pushScript"
}
if ($SkipPush) {
    & $pushScript -RepoRoot $RepoRoot -SkipPush
} else {
    & $pushScript -RepoRoot $RepoRoot
}