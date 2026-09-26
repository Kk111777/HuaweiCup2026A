param(
    [string]$Python = "",
    [string]$Data = "",
    [string]$Config = "",
    [string]$Output = "",
    [int]$LocalTestWorkers = 16,
    [Nullable[double]]$Seconds = $null,
    [string]$Parameters = "",
    [Alias("LocalTestBaselineArchive")]
    [string]$ReportBaselineArchive = "",
    [string]$ArchiveRepoRoot = "",
    [string[]]$ReuseVerifiedRun = @(),
    [string]$BaseSpeedups = "",
    [string]$PersonalReference = "",
    [switch]$OfficialReplay
)
$ErrorActionPreference = "Stop"
$TaskRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $Python) {
    $TaskPython = Join-Path $TaskRoot ".venv\Scripts\python.exe"
    $ParentPython = Join-Path $TaskRoot "..\..\.venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $TaskPython) { $Python = $TaskPython }
    elseif (Test-Path -LiteralPath $ParentPython) { $Python = (Resolve-Path $ParentPython).Path }
    else { $Python = "python" }
}
if (-not $Data) { $Data = Join-Path $TaskRoot "official\data" }
if (-not $Config) { $Config = Join-Path $Data "config.txt" }
if (-not $Output) { $Output = Join-Path $TaskRoot "outputs\unified_100cases" }
if (-not $Parameters) { $Parameters = Join-Path $TaskRoot "configs\unified.json" }
$Parameters = (Resolve-Path -LiteralPath $Parameters).Path
$SearchParameters = Get-Content -LiteralPath $Parameters -Raw -Encoding UTF8 | ConvertFrom-Json
$Diagnostics = $SearchParameters.artifacts.intermediate -ne $false
if (-not $Diagnostics -and ($OfficialReplay -or $ReuseVerifiedRun.Count)) {
    throw "Plan-only mode has no audit/resume metadata; enable artifacts.intermediate for replay/reuse."
}
$Data = (Resolve-Path -LiteralPath $Data).Path
$Config = (Resolve-Path -LiteralPath $Config).Path
& $Python -c "import sys; assert sys.version_info[:2] == (3,12), sys.version"
if ($LASTEXITCODE -ne 0) { throw "Python 3.12 is required." }

$RunArgs = @("-B", "-X", "utf8", (Join-Path $TaskRoot "tools\benchmark_unified.py"),
    "--data", $Data, "--config", $Config, "--output", $Output,
    "--local-test-workers", "$LocalTestWorkers", "--parameters", $Parameters)
if ($null -ne $Seconds) { $RunArgs += @("--seconds", "$Seconds") }
if ($Diagnostics) { $RunArgs += "--resume" }
if ($ReportBaselineArchive) {
    $RunArgs += @("--report-baseline-archive", $ReportBaselineArchive)
    if ($ArchiveRepoRoot) { $RunArgs += @("--archive-repo-root", $ArchiveRepoRoot) }
}
if ($ReuseVerifiedRun.Count) { $RunArgs += @("--reuse-verified-run") + $ReuseVerifiedRun }
# Case list is deliberately omitted: the Python runner defaults to ALL 001-100,
# including case_076. Core counts 2,3,4,5 share ONE case-wide budget.
& $Python @RunArgs
$RunExit = $LASTEXITCODE

if ($OfficialReplay) {
    $AuditArgs = @("-B", "-X", "utf8", (Join-Path $TaskRoot "tools\audit_unified.py"),
        "--run", $Output, "--replay", "--local-test-workers", "4", "--require-current-source")
    if ($BaseSpeedups) { $AuditArgs += @("--base-speedups", $BaseSpeedups) }
    if ($PersonalReference) { $AuditArgs += @("--personal-reference", $PersonalReference) }
    & $Python @AuditArgs
    if ($LASTEXITCODE -ne 0) { throw "Independent official replay failed; inspect the audit output." }
}
elseif ($BaseSpeedups -or $PersonalReference) {
    Write-Warning "Comparison references are only used with -OfficialReplay."
}
if ($RunExit -ne 0) {
    if ($Diagnostics) {
        Write-Warning "Batch contains failed or protected-timeout results. See results.json, case_timings.csv and timings.csv."
    } else {
        Write-Warning "Batch contains failed or protected-timeout results. See console status; plan-only mode does not write diagnostic reports."
    }
    exit $RunExit
}
if ($Diagnostics) {
    Write-Output "100-case execution finished. Case-wide time: case_timings.csv; per-core details: timings.csv. Results: $Output"
} else {
    Write-Output "100-case execution finished. Plan-only results: $Output"
}
