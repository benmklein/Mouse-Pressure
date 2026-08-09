[CmdletBinding()]
param(
    [string]$Python = '',
    [switch]$SkipTests,
    [switch]$SkipInstaller,
    [switch]$SkipKritaPlugin
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$distRoot = Join-Path $repoRoot 'dist'
$appDist = Join-Path $distRoot 'windows'
$pyinstallerWork = Join-Path $repoRoot 'build\pyinstaller'
$spec = Join-Path $repoRoot 'packaging\windows\superstrike_pressure.spec'
$kritaPayload = Join-Path $distRoot 'krita\5.3.3\kritatoolsuperstrikeink.dll'

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

    if (-not $SkipKritaPlugin -and
        -not (Test-Path -LiteralPath $kritaPayload -PathType Leaf)) {
        throw "Krita 5.3.3 plugin payload is missing: $kritaPayload"
    }

    & $Python -m PyInstaller `
        --noconfirm `
        --clean `
        --distpath $appDist `
        --workpath $pyinstallerWork `
        $spec
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }

    $appExe = Join-Path $appDist 'SuperstrikePressure\SuperstrikePressure.exe'
    if (-not (Test-Path -LiteralPath $appExe -PathType Leaf)) {
        throw "Expected application executable was not produced: $appExe"
    }
    Write-Host "Built application: $appExe"

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

    $version = & $Python -c 'from superstrike_pressure import __version__; print(__version__)'
    if ($LASTEXITCODE -ne 0 -or -not $version) {
        throw 'Could not read the application version.'
    }
    & $isccPath "/DMyAppVersion=$version" `
        (Join-Path $repoRoot 'packaging\windows\superstrike_pressure.iss')
    if ($LASTEXITCODE -ne 0) { throw 'Installer build failed.' }
    $installer = Join-Path $distRoot "installer\SuperstrikePressure-$version-Setup.exe"
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
