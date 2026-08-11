[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$PayloadRoot,
    [string]$KritaRoot = '',
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
$supportedVersion = '5.3.3'
$installerScript = Join-Path $PayloadRoot 'install_krita_mouse_pressure.ps1'
$pluginRoot = Join-Path $PayloadRoot $supportedVersion
$pluginPath = Join-Path $pluginRoot 'kritatoolsmousepressure.dll'

function Find-KritaRoot {
    $candidates = [System.Collections.Generic.List[string]]::new()
    foreach ($path in @(
        "$env:ProgramFiles\Krita (x64)",
        "$env:ProgramFiles\Krita"
    )) {
        if ($path) { $candidates.Add($path) }
    }

    foreach ($registryPath in @(
        'HKLM:\Software\Microsoft\Windows\CurrentVersion\App Paths\krita.exe',
        'HKCU:\Software\Microsoft\Windows\CurrentVersion\App Paths\krita.exe'
    )) {
        $entry = Get-ItemProperty -LiteralPath $registryPath -ErrorAction SilentlyContinue
        if ($entry.'(default)') {
            $candidates.Add((Split-Path -Parent (Split-Path -Parent $entry.'(default)')))
        }
    }

    return $candidates |
        Where-Object { Test-Path -LiteralPath (Join-Path $_ 'bin\krita.exe') } |
        Select-Object -Unique -First 1
}

if ($Uninstall) {
    & $installerScript -Uninstall
    exit $LASTEXITCODE
}

if (-not $KritaRoot) {
    $KritaRoot = Find-KritaRoot
}
if (-not $KritaRoot) {
    Write-Warning 'Krita was not detected. The application was installed without the Krita tool.'
    exit 0
}

$kritaExe = Join-Path $KritaRoot 'bin\krita.exe'
$actualVersion = (Get-Item -LiteralPath $kritaExe).VersionInfo.ProductVersion
if (-not $actualVersion.StartsWith($supportedVersion)) {
    Write-Warning (
        "Krita $actualVersion is installed, but this release contains a plugin for " +
        "$supportedVersion. The Krita tool was not installed."
    )
    exit 0
}
if (-not (Test-Path -LiteralPath $pluginPath -PathType Leaf)) {
    throw "Krita plugin payload is missing: $pluginPath"
}

& $installerScript -KritaRoot $KritaRoot -PluginPath $pluginPath
exit $LASTEXITCODE
