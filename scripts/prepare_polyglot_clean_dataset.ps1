param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('java', 'cpp')]
    [string]$Language,

    [Parameter(Mandatory = $true)]
    [string]$BenchmarkRoot,

    [Parameter(Mandatory = $true)]
    [string]$DestinationRoot
)

$ErrorActionPreference = 'Stop'
$practiceRoot = Join-Path (Resolve-Path -LiteralPath $BenchmarkRoot).Path "$Language\exercises\practice"
if (-not (Test-Path -LiteralPath $practiceRoot -PathType Container)) {
    throw "Practice directory not found: $practiceRoot"
}
if (Test-Path -LiteralPath $DestinationRoot) {
    throw "Destination already exists; refusing to overwrite it: $DestinationRoot"
}

New-Item -ItemType Directory -Path $DestinationRoot | Out-Null
$manifestCases = @()
foreach ($case in Get-ChildItem -LiteralPath $practiceRoot -Directory | Sort-Object Name) {
    $configPath = Join-Path $case.FullName '.meta\config.json'
    $instructionsPath = Join-Path $case.FullName '.docs\instructions.md'
    if (-not (Test-Path -LiteralPath $configPath) -or -not (Test-Path -LiteralPath $instructionsPath)) {
        throw "Missing benchmark metadata or instructions for case: $($case.Name)"
    }
    $config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
    $solutionFiles = @($config.files.solution)
    if ($solutionFiles.Count -eq 0) {
        throw "No solution files configured for case: $($case.Name)"
    }

    $targetCase = Join-Path $DestinationRoot $case.Name
    New-Item -ItemType Directory -Path $targetCase | Out-Null
    foreach ($relativePath in $solutionFiles) {
        $sourceFile = Join-Path $case.FullName $relativePath
        if (-not (Test-Path -LiteralPath $sourceFile -PathType Leaf)) {
            throw "Configured solution file is missing: $sourceFile"
        }
        $targetFile = Join-Path $targetCase $relativePath
        New-Item -ItemType Directory -Path (Split-Path -Parent $targetFile) -Force | Out-Null
        Copy-Item -LiteralPath $sourceFile -Destination $targetFile
    }

    # Java source compilation requires the Gradle wrapper and build descriptor, but not
    # .gradle/build caches, tests, metadata, or reference solutions.
    if ($Language -eq 'java') {
        foreach ($supportPath in @('build.gradle', 'gradlew', 'gradlew.bat')) {
            $sourceSupport = Join-Path $case.FullName $supportPath
            if (Test-Path -LiteralPath $sourceSupport -PathType Leaf) {
                Copy-Item -LiteralPath $sourceSupport -Destination (Join-Path $targetCase $supportPath)
            }
        }
        $gradleSource = Join-Path $case.FullName 'gradle'
        if (Test-Path -LiteralPath $gradleSource -PathType Container) {
            Copy-Item -LiteralPath $gradleSource -Destination (Join-Path $targetCase 'gradle') -Recurse
        }
    }

    $promptParts = @()
    $introductionPath = Join-Path $case.FullName '.docs\introduction.md'
    if (Test-Path -LiteralPath $introductionPath) { $promptParts += Get-Content -LiteralPath $introductionPath -Raw }
    $promptParts += Get-Content -LiteralPath $instructionsPath -Raw
    $appendPath = Join-Path $case.FullName '.docs\instructions.append.md'
    if (Test-Path -LiteralPath $appendPath) { $promptParts += Get-Content -LiteralPath $appendPath -Raw }
    $filesText = ($solutionFiles | ForEach-Object { "- ``$_``" }) -join [Environment]::NewLine
    $promptParts += @"
## Evaluation constraints

Implement the task by modifying only these files in the current workspace:
$filesText

Tests and reference implementations are intentionally unavailable. Do not use network access.
"@
    Set-Content -LiteralPath (Join-Path $targetCase 'PROMPT.md') -Value ($promptParts -join [Environment]::NewLine) -Encoding utf8NoBOM

    $manifestCases += [ordered]@{ id = $case.Name; prompt_file = "$($case.Name)/PROMPT.md"; editable_files = @($solutionFiles) }
}

$manifest = [ordered]@{
    benchmark = "Aider Polyglot $Language"
    language = $Language
    source_root = $practiceRoot
    case_count = $manifestCases.Count
    cases = $manifestCases
}
$manifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $DestinationRoot 'manifest.json') -Encoding utf8NoBOM
Write-Output "Created clean $Language dataset with $($manifestCases.Count) cases: $DestinationRoot"
