<# Tests saved Java benchmark answers without invoking StellarCode or an LLM. #>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ResultsRoot,

    [ValidateRange(1, 99)]
    [int]$Attempt = 1,

    [string]$SummaryFile,

    [switch]$Quiet
)

$ErrorActionPreference = "Continue"
if (-not (Get-Command cmd.exe -ErrorAction SilentlyContinue)) {
    throw "cmd.exe is required to run Gradle on Windows."
}

$resolvedResultsRoot = (Resolve-Path -LiteralPath $ResultsRoot).Path
$casesRoot = Join-Path $resolvedResultsRoot "cases"
if (-not (Test-Path -LiteralPath $casesRoot -PathType Container)) { throw "No cases directory found: $casesRoot" }
if (-not $SummaryFile) { $SummaryFile = Join-Path $resolvedResultsRoot "java-test-summary-attempt-$Attempt.json" }

$rows = foreach ($case in Get-ChildItem -LiteralPath $casesRoot -Directory | Sort-Object Name) {
    $attemptDir = Join-Path $case.FullName "attempt-$Attempt"
    $agentResult = Join-Path $attemptDir "agent-result.json"
    $testDir = Join-Path $attemptDir "private-tests"
    if (-not (Test-Path -LiteralPath $agentResult -PathType Leaf)) {
        [PSCustomObject]@{ case = $case.Name; passed = $false; skipped = $true; exit_code = $null; output = "No completed Agent result." }
        continue
    }
    if (-not (Test-Path -LiteralPath $testDir -PathType Container)) {
        [PSCustomObject]@{ case = $case.Name; passed = $false; skipped = $true; exit_code = $null; output = "Private test copy is missing." }
        continue
    }
    if (-not $Quiet) { Write-Host "`n=== $($case.Name) ===" -ForegroundColor Cyan }
    Push-Location -LiteralPath $testDir
    try {
        $text = (& cmd.exe /d /c gradlew.bat test --no-daemon 2>&1 | Out-String)
        $exitCode = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }
    if (-not $Quiet) { Write-Host $text }
    if ($text.Length -gt 20000) { $text = $text.Substring($text.Length - 20000) }
    [PSCustomObject]@{ case = $case.Name; passed = ($exitCode -eq 0); skipped = $false; exit_code = $exitCode; output = $text }
}

$tested = @($rows | Where-Object { -not $_.skipped })
$passed = @($tested | Where-Object { $_.passed }).Count
$summary = [PSCustomObject]@{
    benchmark = "Aider Polyglot Java / StellarCode Agent"
    source = "saved agent answers; no Agent or API calls"
    attempt = $Attempt
    case_count = $rows.Count
    tested_case_count = $tested.Count
    passed = $passed
    pass_rate = if ($tested.Count) { [math]::Round(100 * $passed / $tested.Count, 2) } else { 0.0 }
    skipped = @($rows | Where-Object { $_.skipped }).Count
    cases = $rows
}
$summary | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $SummaryFile -Encoding utf8
$rows | Select-Object case, passed, skipped, exit_code | Format-Table -AutoSize
Write-Host "`nSummary: $passed/$($tested.Count) passed ($($summary.pass_rate)%)"
Write-Host "Saved: $SummaryFile"
if ($passed -ne $tested.Count) { exit 1 }
