# Commit and push vps_full_reports/latest/ (including report.docx) to origin/main.
param(
    [switch]$SkipPush,
    [string]$RepoRoot = ""
)

$ErrorActionPreference = "Stop"
if (-not $RepoRoot) {
    $RepoRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
}

$Latest = Join-Path $RepoRoot "vps_full_reports\latest"
$Required = @(
    "FULL_REPORT.md",
    "report.md",
    "report.docx",
    "btc_pulse_status.json",
    "btc_pulse_ledger.json",
    "btc_pulse_light_report.json"
)

foreach ($f in $Required) {
    $p = Join-Path $Latest $f
    if (-not (Test-Path $p)) {
        Write-Error "Cannot push report: missing $f in vps_full_reports/latest/"
    }
}

Push-Location $RepoRoot
try {
    $statusPath = Join-Path $Latest "btc_pulse_status.json"
    $settled = "?"
    $wr = "?"
    $pf = "?"
    try {
        $raw = Get-Content $statusPath -Raw -Encoding UTF8
        if ($raw -match '"settled"\s*:\s*(\d+)') { $settled = $Matches[1] }
        if ($raw -match '"win_rate"\s*:\s*([0-9.]+)') { $wr = "{0:P1}" -f [double]$Matches[1] }
        if ($raw -match '"profit_factor"\s*:\s*([0-9.]+)') { $pf = $Matches[1] }
    } catch {
        Write-Warning "Could not parse status JSON for commit message: $($_.Exception.Message)"
    }

    $tracked = @(git ls-files "vps_full_reports/latest/")
    foreach ($rel in $tracked) {
        $local = Join-Path $RepoRoot ($rel -replace '/', '\')
        if (-not (Test-Path $local)) {
            git rm -f -- $rel
            Write-Host "Removed stale tracked file: $rel"
        }
    }

    git add -f "vps_full_reports/latest/"
    git add -f "monitoring/timeline.jsonl" "monitoring/latest-snapshot.json"
    git add -f "monitoring/technical-grades.json" "monitoring/grades-history.jsonl" "monitoring/TECHNICAL_GRADES.md" "monitoring/TECHNICAL_REPORT.md"

    $staged = git diff --cached --name-only
    if (-not $staged) {
        Write-Host "Report unchanged - nothing to commit"
        return
    }

    $grade = "?"
    $gradePath = Join-Path $RepoRoot "monitoring\technical-grades.json"
    if (Test-Path $gradePath) {
        try {
            $g = Get-Content $gradePath -Raw -Encoding UTF8 | ConvertFrom-Json
            $grade = $g.composite.grade
        } catch {
            Write-Warning "Could not parse technical grades for commit message"
        }
    }

    $ts = (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd HH:mm") + " UTC"
    $msg = ('chore(reports): VPS full report {0} ({1} settled, WR {2}, PF {3}, grade {4})' -f $ts, $settled, $wr, $pf, $grade)
    git commit -m $msg

    if ($SkipPush) {
        Write-Host ('Committed locally (SkipPush): ' + $msg)
        return
    }

    git push origin main
    Write-Host ('Pushed report to origin/main: ' + $msg)
} finally {
    Pop-Location
}