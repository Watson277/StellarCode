[CmdletBinding()]
param(
    # Opt in to downloading PyInstaller when the release build environment does
    # not have it yet. The default never changes the developer virtualenv.
    [switch]$InstallBuildTools
)

$ErrorActionPreference = "Stop"

if ($env:OS -ne "Windows_NT") {
    throw "This script builds the supported Windows x64 installer and must run on Windows."
}

$DesktopRoot = Split-Path -Parent $PSScriptRoot
$RepositoryRoot = Split-Path -Parent $DesktopRoot
$Python = Join-Path $RepositoryRoot ".venv\Scripts\python.exe"
$EntryPoint = Join-Path $RepositoryRoot "scripts\sidecar_entry.py"
$ResourcesRoot = Join-Path $DesktopRoot "src-tauri\resources"
$RuntimeResource = Join-Path $ResourcesRoot "stellarcode-sidecar"
$BuildRoot = Join-Path $DesktopRoot "src-tauri\target\release-build"
$StageDist = Join-Path $BuildRoot "pyinstaller-dist"

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Missing release Python: $Python. Create the project .venv and install project dependencies first."
}

& $Python -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    if (-not $InstallBuildTools) {
        throw "PyInstaller is not installed. Run: .\scripts\build-windows-release.ps1 -InstallBuildTools"
    }
    & $Python -m pip install "pyinstaller>=6,<7"
}

if (Test-Path -LiteralPath $RuntimeResource) {
    # This is a generated staging directory owned solely by this script.
    Remove-Item -LiteralPath $RuntimeResource -Recurse -Force
}
New-Item -ItemType Directory -Path $RuntimeResource -Force | Out-Null
New-Item -ItemType Directory -Path $BuildRoot -Force | Out-Null

$PyInstallerArguments = @(
    "--noconfirm",
    "--clean",
    "--onedir",
    "--name", "stellarcode-sidecar",
    "--paths", (Join-Path $RepositoryRoot "src"),
    "--distpath", $StageDist,
    "--workpath", (Join-Path $BuildRoot "pyinstaller-work"),
    "--specpath", (Join-Path $BuildRoot "pyinstaller-spec"),
    "--collect-all", "stellarcode",
    # Do not collect `mcp.cli`: it is an optional SDK command-line surface that
    # pulls in typer, while StellarCode only embeds MCP client transports.
    "--collect-submodules", "mcp.client",
    "--collect-all", "jieba",
    # BeautifulSoup selects this builder dynamically by name, so make both the
    # Python adapter and lxml's compiled Windows extensions explicit in releases.
    "--hidden-import", "bs4.builder._lxml",
    "--collect-all", "lxml",
    "--collect-all", "PIL",
    "--copy-metadata", "mcp",
    $EntryPoint
)

Write-Host "[1/2] Bundling the Python Runtime Sidecar..." -ForegroundColor Cyan
& $Python -m PyInstaller @PyInstallerArguments
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE."
}

$SidecarOutput = Join-Path $StageDist "stellarcode-sidecar"
if (-not (Test-Path -LiteralPath (Join-Path $SidecarOutput "stellarcode-sidecar.exe") -PathType Leaf)) {
    throw "PyInstaller completed without stellarcode-sidecar.exe."
}
Copy-Item -Path (Join-Path $SidecarOutput "*") -Destination $RuntimeResource -Recurse -Force

Write-Host "[2/2] Building Tauri installer..." -ForegroundColor Cyan
Push-Location $DesktopRoot
try {
    npm run tauri build
    if ($LASTEXITCODE -ne 0) {
        throw "Tauri build failed with exit code $LASTEXITCODE."
    }
} finally {
    Pop-Location
}

$BundleRoot = Join-Path $DesktopRoot "src-tauri\target\release\bundle"
Write-Host "Release build complete. Installers are under: $BundleRoot" -ForegroundColor Green
Get-ChildItem -LiteralPath $BundleRoot -Recurse -File |
    Where-Object { $_.Extension -in ".exe", ".msi" } |
    Select-Object FullName, Length
