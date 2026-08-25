<#
.SYNOPSIS
Tests already-generated Aider Polyglot C++ answers without calling StellarCode.

.DESCRIPTION
Each selected attempt must already contain private-tests, populated by
benchmark-run after the Agent completed. This script only compiles and tests
those saved copies; it never calls an LLM or changes the source datasets.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ResultsRoot,

    [ValidateRange(1, 99)]
    [int]$Attempt = 1,

    [string]$SummaryFile,

    [switch]$KeepBuild,

    [switch]$Quiet
)

# Native tools often write ordinary warnings to stderr. Keep those warnings in
# the per-case log and determine success solely from their exit code.
$ErrorActionPreference = "Continue"

foreach ($tool in "cmake", "ctest") {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        throw "Required test tool '$tool' was not found. Install CMake, reopen PowerShell, then run 'cmake --version'."
    }
}

$resolvedResultsRoot = (Resolve-Path -LiteralPath $ResultsRoot).Path
$casesRoot = Join-Path $resolvedResultsRoot "cases"
if (-not (Test-Path -LiteralPath $casesRoot -PathType Container)) {
    throw "No cases directory found: $casesRoot"
}
if (-not $SummaryFile) {
    $SummaryFile = Join-Path $resolvedResultsRoot "cpp-test-summary-attempt-$Attempt.json"
}
$attemptName = "attempt-$Attempt"
$rows = @()

foreach ($case in Get-ChildItem -LiteralPath $casesRoot -Directory | Sort-Object Name) {
    $attemptDir = Join-Path $case.FullName $attemptName
    $testDir = Join-Path $attemptDir "private-tests"
    $buildDir = Join-Path $testDir "build"
    $commands = @()
    $output = @()
    $exitCode = $null

    if (-not (Test-Path -LiteralPath $testDir -PathType Container)) {
        $rows += [PSCustomObject]@{
            case = $case.Name; attempt = $Attempt; passed = $false; skipped = $true
            exit_code = $null; output = "Missing saved private-test directory: $testDir"; commands = @()
        }
        continue
    }
    if (-not (Test-Path -LiteralPath (Join-Path $testDir "CMakeLists.txt") -PathType Leaf)) {
        $rows += [PSCustomObject]@{
            case = $case.Name; attempt = $Attempt; passed = $false; skipped = $true
            exit_code = $null; output = "Missing CMakeLists.txt: $testDir"; commands = @()
        }
        continue
    }

    # The upstream CMakeLists derives the exercise name from its source
    # directory. Our private copy is intentionally named "private-tests", so
    # pin the original case name in this disposable copy before configuring.
    $cmakeLists = Join-Path $testDir "CMakeLists.txt"
    $cmakeContents = [System.IO.File]::ReadAllText($cmakeLists)
    $derivedName = 'get_filename_component(exercise ${CMAKE_CURRENT_SOURCE_DIR} NAME)'
    if ($cmakeContents.Contains($derivedName)) {
        $updatedCmakeContents = $cmakeContents.Replace($derivedName, "set(exercise `"$($case.Name)`")")
        [System.IO.File]::WriteAllText($cmakeLists, $updatedCmakeContents, [System.Text.UTF8Encoding]::new($false))
    }

    if (-not $KeepBuild -and (Test-Path -LiteralPath $buildDir -PathType Container)) {
        $resolvedBuildDir = (Resolve-Path -LiteralPath $buildDir).Path
        $resolvedAttemptDir = (Resolve-Path -LiteralPath $attemptDir).Path
        if (-not $resolvedBuildDir.StartsWith($resolvedAttemptDir, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to remove a build directory outside this saved attempt: $resolvedBuildDir"
        }
        Remove-Item -LiteralPath $resolvedBuildDir -Recurse -Force
    }

    if (-not $Quiet) { Write-Host "`n=== $($case.Name) ===" -ForegroundColor Cyan }
    $commands += "cmake -S . -B build"
    $text = (& cmake -S $testDir -B $buildDir 2>&1 | Out-String)
    $output += "$ cmake -S . -B build`n$text"
    if (-not $Quiet) { Write-Host $text }
    $exitCode = $LASTEXITCODE

    if ($exitCode -eq 0) {
        $commands += "cmake --build build"
        $text = (& cmake --build $buildDir 2>&1 | Out-String)
        $output += "$ cmake --build build`n$text"
        if (-not $Quiet) { Write-Host $text }
        $exitCode = $LASTEXITCODE
    }
    if ($exitCode -eq 0) {
        $commands += "ctest --test-dir build --output-on-failure"
        $text = (& ctest --test-dir $buildDir --output-on-failure 2>&1 | Out-String)
        $output += "$ ctest --test-dir build --output-on-failure`n$text"
        if (-not $Quiet) { Write-Host $text }
        $exitCode = $LASTEXITCODE
    }

    $combinedOutput = $output -join "`n"
    if ($combinedOutput.Length -gt 20000) {
        $combinedOutput = $combinedOutput.Substring($combinedOutput.Length - 20000)
    }
    $rows += [PSCustomObject]@{
        case = $case.Name; attempt = $Attempt; passed = ($exitCode -eq 0); skipped = $false
        exit_code = $exitCode; output = $combinedOutput; commands = $commands
    }
}

$passed = @($rows | Where-Object { $_.passed }).Count
$summary = [PSCustomObject]@{
    benchmark = "Aider Polyglot C++ / StellarCode Agent"
    source = "saved agent answers; no Agent or API calls"
    attempt = $Attempt
    case_count = $rows.Count
    passed = $passed
    pass_rate = if ($rows.Count) { [math]::Round(100 * $passed / $rows.Count, 2) } else { 0.0 }
    skipped = @($rows | Where-Object { $_.skipped }).Count
    cases = $rows
}

$summary | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $SummaryFile -Encoding utf8
$rows | Select-Object case, passed, skipped, exit_code | Format-Table -AutoSize
Write-Host "`nSummary: $passed/$($rows.Count) passed ($($summary.pass_rate)%)" -ForegroundColor Green
Write-Host "Saved: $SummaryFile"

if ($passed -ne $rows.Count) { exit 1 }
