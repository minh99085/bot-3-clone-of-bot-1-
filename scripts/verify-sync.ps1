# Exit 0 if origin/main == VPS HEAD; exit 1 if diverged.
param(
    [string]$SshKey = "$env:USERPROFILE\.ssh\bot1_grok_temp",
    [string]$VpsHost = "144.202.122.120",
    [string]$VpsUser = "root",
    [string]$VpsRepo = "/opt/Bot-1"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path $PSScriptRoot -Parent
Set-Location $RepoRoot

$prevEap = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& git fetch origin main 2>&1 | Out-Null
$ErrorActionPreference = $prevEap

$origin = ("$(git rev-parse origin/main)".Trim()).ToLowerInvariant()
$vpsHead = (& ssh.exe -i $SshKey -o ConnectTimeout=20 -o StrictHostKeyChecking=no "${VpsUser}@${VpsHost}" "git -C $VpsRepo rev-parse HEAD").Trim().ToLowerInvariant()

Write-Host "origin/main : $($origin.Substring(0,7)) $origin"
Write-Host "VPS HEAD    : $($vpsHead.Substring(0,7)) $vpsHead"

if ($vpsHead -eq $origin) {
    Write-Host "VERIFY OK - VPS matches origin/main."
    exit 0
}
Write-Error "VERIFY FAIL - VPS diverged from origin/main."
exit 1