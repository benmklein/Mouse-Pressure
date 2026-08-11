[CmdletBinding()]
param(
    [string]$Python = '',
    [switch]$SkipTests,
    [switch]$SkipInstaller
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$distRoot = Join-Path $repoRoot 'dist'
$appDist = Join-Path $distRoot 'windows'
$pyinstallerWork = Join-Path $repoRoot 'build\pyinstaller'
$spec = Join-Path $repoRoot 'packaging\windows\mouse_pressure.spec'
$sandboxSpec = Join-Path $repoRoot 'packaging\windows\mouse_pressure_sandbox.spec'

function Copy-ReleaseNotices([string]$Target) {
    $legalTarget = Join-Path $Target 'legal'
    $docsTarget = Join-Path $Target 'docs'
    New-Item -ItemType Directory -Force -Path $legalTarget, $docsTarget | Out-Null
    @('LICENSE', 'LICENSING.md', 'THIRD_PARTY_NOTICES.md', 'PRIVACY.md', 'SECURITY.md') |
        ForEach-Object {
            Copy-Item -LiteralPath (Join-Path $repoRoot $_) -Destination $Target -Force
        }
    Copy-Item -Path (Join-Path $repoRoot 'packaging\legal\*') `
        -Destination $legalTarget -Recurse -Force
    Copy-Item -Path (Join-Path $repoRoot 'dist\release-metadata\*') `
        -Destination $legalTarget -Recurse -Force
    @('compatibility.md', 'recovery.md') | ForEach-Object {
        Copy-Item -LiteralPath (Join-Path $repoRoot "docs\$_") `
            -Destination $docsTarget -Force
    }
}

if (-not $Python) {
    $venvPython = Join-Path $repoRoot '.venv\Scripts\python.exe'
    $Python = if (Test-Path -LiteralPath $venvPython) { $venvPython } else { 'python' }
}

Push-Location $repoRoot
try {
    if (-not $SkipTests) {
        & $Python -m pytest
        if ($LASTEXITCODE -ne 0) { throw 'Tests failed.' }
    }

    & $Python -c 'import PyInstaller' 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw 'PyInstaller is not installed. Run: python -m pip install -e ".[release]"'
    }
    & $Python -c 'import pygame' 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw 'pygame-ce is not installed. Run: python -m pip install -e ".[release,sandbox]"'
    }

    & (Join-Path $repoRoot 'scripts\build_native_relay.ps1') -BootstrapZig
    if ($LASTEXITCODE -ne 0) { throw 'Native synthetic relay build failed.' }

    & $Python scripts\vendor_release_licenses.py
    if ($LASTEXITCODE -ne 0) { throw 'Release license bundle validation failed.' }
    & $Python scripts\check_public_artifacts.py
    if ($LASTEXITCODE -ne 0) { throw 'Public-artifact privacy check failed.' }
    & $Python scripts\generate_release_metadata.py
    if ($LASTEXITCODE -ne 0) { throw 'Release metadata generation failed.' }

    & $Python -m PyInstaller `
        --noconfirm `
        --clean `
        --distpath $appDist `
        --workpath $pyinstallerWork `
        $spec
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }

    $appExe = Join-Path $appDist 'MousePressure\MousePressure.exe'
    if (-not (Test-Path -LiteralPath $appExe -PathType Leaf)) {
        throw "Expected application executable was not produced: $appExe"
    }
    Copy-ReleaseNotices -Target (Split-Path -Parent $appExe)
    Write-Host "Built application: $appExe"

    & $Python -m PyInstaller `
        --noconfirm `
        --clean `
        --distpath $appDist `
        --workpath (Join-Path $pyinstallerWork 'sandbox') `
        $sandboxSpec
    if ($LASTEXITCODE -ne 0) { throw 'Sandbox PyInstaller build failed.' }

    $sandboxExe = Join-Path $appDist 'MousePressureSandbox\MousePressureSandbox.exe'
    if (-not (Test-Path -LiteralPath $sandboxExe -PathType Leaf)) {
        throw "Expected sandbox executable was not produced: $sandboxExe"
    }
    Copy-ReleaseNotices -Target (Split-Path -Parent $sandboxExe)
    Write-Host "Built sandbox: $sandboxExe"

    if ($SkipInstaller) { return }

    $iscc = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if (-not $iscc) {
        $knownPaths = @(
            "$env:ProgramFiles\Inno Setup 7\ISCC.exe",
            "$env:LOCALAPPDATA\Programs\Inno Setup 7\ISCC.exe",
            "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
        )
        $isccPath = $knownPaths | Where-Object { Test-Path -LiteralPath $_ } |
            Select-Object -First 1
    } else {
        $isccPath = $iscc.Source
    }
    if (-not $isccPath) {
        throw 'Inno Setup compiler was not found. Install Inno Setup or use -SkipInstaller.'
    }

    $version = & $Python -c 'from mouse_pressure import __version__; print(__version__)'
    if ($LASTEXITCODE -ne 0 -or -not $version) {
        throw 'Could not read the application version.'
    }
    & $isccPath "/DMyAppVersion=$version" `
        (Join-Path $repoRoot 'packaging\windows\mouse_pressure.iss')
    if ($LASTEXITCODE -ne 0) { throw 'Installer build failed.' }
    $installer = Join-Path $distRoot "installer\MousePressure-$version-Setup.exe"
    if (-not (Test-Path -LiteralPath $installer -PathType Leaf)) {
        throw "Expected installer was not produced: $installer"
    }
    $checksum = Get-FileHash -LiteralPath $installer -Algorithm SHA256
    $checksumPath = $installer + '.sha256'
    Set-Content -LiteralPath $checksumPath -Encoding ascii -Value (
        $checksum.Hash.ToLowerInvariant() + '  ' + (Split-Path -Leaf $installer)
    )
    Write-Host "Built installer: $installer"
    Write-Host "Wrote checksum: $checksumPath"
} finally {
    Pop-Location
}
