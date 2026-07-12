# One-time: grant this cloud agent SSH access to Bot 3 VPS.
# Run from laptop (has hermes-laptop-vps private key + latest repo).
[CmdletBinding()]
param(
    [string]$SshKey = "$env:USERPROFILE\.ssh\hermes-laptop-vps",
    [string]$VpsHost = "207.246.96.45",
    [string]$VpsUser = "root"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path $PSScriptRoot -Parent
$PubFile = Join-Path $RepoRoot "scripts\keys\bot3-cloud-agent.pub"

if (-not (Test-Path $PubFile)) {
    Write-Error "Missing $PubFile — git pull origin main first."
}
if (-not (Test-Path $SshKey)) {
    Write-Error "Missing laptop private key: $SshKey"
}

$pub = (Get-Content $PubFile -Raw).Trim()
$escaped = $pub.Replace("'", "'\''")

Write-Host "Granting cloud-agent SSH on $VpsUser@${VpsHost}..."
& ssh.exe -i $SshKey -o ConnectTimeout=20 -o StrictHostKeyChecking=no "${VpsUser}@${VpsHost}" @"
grep -qF 'bot3-cloud-agent' ~/.ssh/authorized_keys 2>/dev/null || echo '$escaped' >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
grep bot3-cloud-agent ~/.ssh/authorized_keys
"@

Write-Host "Done. Cloud agents can now SSH with scripts/keys/bot3-cloud-agent (private key on agent VM)."
