# Sync origin/main -> Bot 1 VPS, then ALWAYS down --remove-orphans -> build -> up --remove-orphans.
# Operator memory: ALWAYS remove orphans and rebuild after VPS sync (default; never -SkipRebuild
# unless operator explicitly requests code-only sync in the current message).
# Policy: .grok/rules/vps-deploy-mandatory.md — never push without running this (except hands_off).
[CmdletBinding()]
param(
    [switch]$SkipRebuild,
    [switch]$Rebuild,
    [switch]$VerifyOnly,
    [string]$SshKey = "$env:USERPROFILE\.ssh\bot1_grok_temp",
    [string]$VpsHost = "144.202.122.120",
    [string]$VpsUser = "root",
    [string]$VpsRepo = "/opt/Bot-1",
    [string]$PluginPath = "/opt/Bot-1/hermes-agent-main/plugins/hermes-trading-engine"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path $PSScriptRoot -Parent
if (-not (Test-Path (Join-Path $RepoRoot ".git"))) {
    Write-Error "Not a git repo: $RepoRoot"
}
if ($RepoRoot -notmatch "Bot-1") {
    Write-Error "SAFETY: sync-vps.ps1 deploys Bot-1 (standalone) only."
}
Set-Location $RepoRoot

if ($VerifyOnly) {
    & "$PSScriptRoot\verify-sync.ps1"
    exit $LASTEXITCODE
}

function Get-ShortSha([string]$sha) { if ($sha.Length -ge 7) { $sha.Substring(0, 7) } else { $sha } }

function Invoke-SshCmd([string]$RemoteCmd) {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $out = & ssh.exe -i $SshKey -o ConnectTimeout=20 -o StrictHostKeyChecking=no "${VpsUser}@${VpsHost}" $RemoteCmd
    $ErrorActionPreference = $prev
    if ($null -eq $out) { return "" }
    if ($out -is [array]) { return ($out[-1] | Out-String).Trim() }
    return "$out".Trim()
}

function Invoke-SshScript([string]$Body) {
    $localScript = Join-Path $env:TEMP "grok-bot1-remote-$([Guid]::NewGuid().ToString('N')).sh"
    $remoteScript = "/tmp/grok-bot1-remote-$([Guid]::NewGuid().ToString('N')).sh"
    # Unix LF only — CRLF breaks `set -e` on Linux remote scripts.
    $utf8NoBom = New-Object System.Text.UTF8Encoding $false
    [IO.File]::WriteAllText($localScript, ($Body -replace "`r`n", "`n"), $utf8NoBom)
    & scp.exe -i $SshKey -o StrictHostKeyChecking=no $localScript "${VpsUser}@${VpsHost}:$remoteScript"
    Invoke-SshCmd "bash $remoteScript; rm -f $remoteScript"
    Remove-Item $localScript -Force -ErrorAction SilentlyContinue
}

$doRebuild = -not $SkipRebuild
Write-Host "BOT1 deploy -> $VpsUser@${VpsHost}:$VpsRepo"

$prevEap = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& git fetch origin main 2>&1 | Out-Null
$ErrorActionPreference = $prevEap
$local = (git rev-parse HEAD).Trim()
$origin = (git rev-parse origin/main 2>$null).Trim()
if (-not $origin) { $origin = $local }

if ($local -ne $origin) {
    $mergeBase = (git merge-base HEAD origin/main 2>$null).Trim()
    if ($mergeBase -eq $local -and $local -ne $origin) {
        Write-Host "Local behind origin/main — fast-forward pull..."
        git pull --ff-only origin main
        $local = (git rev-parse HEAD).Trim()
    }
    if ($local -ne $origin) {
        Write-Error "Local HEAD ($local) != origin/main ($origin). Push or pull first."
    }
}

$origin = "$origin".Trim().ToLowerInvariant()

$vpsHead = (& ssh.exe -i $SshKey -o ConnectTimeout=20 -o StrictHostKeyChecking=no "${VpsUser}@${VpsHost}" "git -C $VpsRepo rev-parse HEAD").Trim().ToLowerInvariant()
if ($vpsHead -notmatch '^[0-9a-f]{40}$') {
    Start-Sleep -Seconds 2
    $vpsHead = (& ssh.exe -i $SshKey -o ConnectTimeout=20 -o StrictHostKeyChecking=no "${VpsUser}@${VpsHost}" "git -C $VpsRepo rev-parse HEAD").Trim().ToLowerInvariant()
    if ($vpsHead -notmatch '^[0-9a-f]{40}$') { $vpsHead = "MISSING" }
}

Write-Host "origin/main : $(Get-ShortSha $origin) $origin"
Write-Host "VPS HEAD    : $(Get-ShortSha $vpsHead) $vpsHead"

if ($vpsHead -eq $origin) {
    Write-Host "SYNC OK - VPS already matches origin/main."
    if (-not $doRebuild) {
        Write-Warning "SkipRebuild set — containers NOT rebuilt (operator override only)."
        exit 0
    }
    Write-Host "REBUILD - code current; running orphan cleanup + full rebuild per deploy policy."
}

if ($vpsHead -eq "MISSING" -or $vpsHead.Length -ne 40) {
    Write-Host "Bootstrap VPS repo (git HEAD unavailable on first probe)..."
    $bootstrap = @"
set -e
sudo mkdir -p $VpsRepo
sudo chown -R ${VpsUser}:${VpsUser} $VpsRepo
if [ ! -d $VpsRepo/.git ]; then
  git clone https://github.com/minh99085/Bot-1.git $VpsRepo
fi
cd $VpsRepo
git fetch origin main
git reset --hard origin/main
git clean -fd
echo VPS_HEAD=`$(git rev-parse HEAD)
"@
    Invoke-SshScript $bootstrap
    $vpsHead = (Invoke-SshCmd "git -C $VpsRepo rev-parse HEAD").Trim()
}

$bundle = Join-Path $env:TEMP "grok-bot1-sync.bundle"
if ($vpsHead -ne $origin) {
    Write-Host "Creating bundle $vpsHead..$origin ..."
    & git bundle create $bundle "HEAD" "^$vpsHead"
    if (-not (Test-Path $bundle)) {
        Write-Error "Bundle creation failed. VPS=$vpsHead origin=$origin"
    }
    & scp.exe -i $SshKey -o StrictHostKeyChecking=no $bundle "${VpsUser}@${VpsHost}:/tmp/grok-bot1-sync.bundle"
    $remote = @"
set -e
cd $VpsRepo
git fetch /tmp/grok-bot1-sync.bundle HEAD:refs/remotes/bundle/main
git reset --hard bundle/main
git clean -fd
rm -f /tmp/grok-bot1-sync.bundle
echo VPS_HEAD=`$(git rev-parse HEAD)
"@
    Invoke-SshScript $remote
    Remove-Item -Force $bundle -ErrorAction SilentlyContinue
}

if ($doRebuild) {
    $docker = @"
set -e
cd $VpsRepo
python3 scripts/apply-loop-arch-env.py
python3 scripts/pulse-babysit/validate-frozen-lock.py || exit 1
cd $PluginPath
docker compose down --remove-orphans
docker compose build
docker compose up -d --force-recreate --remove-orphans
sleep 8
docker ps --format '{{.Names}} {{.Status}}' | grep -E 'hermes-training|hermes-trading-engine'
"@
    Invoke-SshScript $docker
}

$vpsAfter = (Invoke-SshCmd "git -C $VpsRepo rev-parse HEAD").Trim()
if ($vpsAfter -ne $origin) {
    Write-Error "SYNC FAIL after deploy: VPS=$vpsAfter origin=$origin"
}

Write-Host "BOT1 SYNC OK - VPS HEAD matches origin/main ($(Get-ShortSha $origin))."
Write-Host "VERIFY - re-checking VPS HEAD vs origin/main..."
& "$PSScriptRoot\verify-sync.ps1"
exit $LASTEXITCODE