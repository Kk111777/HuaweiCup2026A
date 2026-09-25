param(
    [string]$Python = "",
    [string]$Data = "",
    [string]$Config = "",
    [string]$Output = "",
    [int]$LocalTestWorkers = 16,
    [string]$LocalTestBaselineArchive = "",
    [string]$ArchiveRepoRoot = "",
    [string[]]$ReuseVerifiedRun = @(),
    [string]$BaseSpeedups = "",
    [string]$PersonalReference = ""
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
$Data = (Resolve-Path -LiteralPath $Data).Path
$Config = (Resolve-Path -LiteralPath $Config).Path
& $Python -c "import sys; assert sys.version_info[:2] == (3,12), sys.version"
if ($LASTEXITCODE -ne 0) { throw "Python 3.12 is required." }

$RunArgs = @("-B", "-X", "utf8", (Join-Path $TaskRoot "tools\benchmark_unified.py"),
    "--data", $Data, "--config", $Config, "--output", $Output,
    "--local-test-workers", "$LocalTestWorkers", "--resume")
if ($LocalTestBaselineArchive) {
    $RunArgs += @("--local-test-baseline-archive", $LocalTestBaselineArchive)
    if ($ArchiveRepoRoot) { $RunArgs += @("--archive-repo-root", $ArchiveRepoRoot) }
}
if ($ReuseVerifiedRun.Count) { $RunArgs += @("--reuse-verified-run") + $ReuseVerifiedRun }
# Case list is deliberately omitted: the Python runner defaults to ALL 001-100,
# including case_076, and sequentially solves core counts 2,3,4,5 within each case.
& $Python @RunArgs
$RunExit = $LASTEXITCODE

$AuditArgs = @("-B", "-X", "utf8", (Join-Path $TaskRoot "tools\audit_unified.py"),
    "--run", $Output, "--replay", "--local-test-workers", "4", "--require-current-source")
if ($BaseSpeedups) { $AuditArgs += @("--base-speedups", $BaseSpeedups) }
if ($PersonalReference) { $AuditArgs += @("--personal-reference", $PersonalReference) }
& $Python @AuditArgs
if ($LASTEXITCODE -ne 0) { throw "Independent official replay failed; inspect the audit output." }
& $Python -B -X utf8 (Join-Path $TaskRoot "tools\report_unified.py") --run $Output
if ($LASTEXITCODE -ne 0) { throw "Report generation failed; execution outputs are preserved." }
if ($RunExit -ne 0) {
    Write-Warning "Batch contains failed or protected-timeout results. See results.json and timings.csv."
    exit $RunExit
}
Write-Output "100-case execution and official replay finished. Results: $Output"
