# Copy polymarket training data to your Samsung T7 (or any drive).
# Run in PowerShell on your laptop AFTER the cloud agent finishes the download.
#
# Example:
#   .\scripts\polymarket-backfill\copy-to-drive.ps1 -Source "\\server\share\polymarket-training" -Dest "D:\polymarket-training"

param(
    [string]$Source = ".\data\polymarket-training",
    [string]$Dest = "D:\polymarket-training"
)

$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Force -Path $Dest | Out-Null
Write-Host "Copying $Source -> $Dest"
robocopy $Source $Dest /E /Z /R:3 /W:5 /MT:8 /NFL /NDL /NP
if ($LASTEXITCODE -ge 8) { exit $LASTEXITCODE }
Write-Host "Done. Training data is on $Dest"
