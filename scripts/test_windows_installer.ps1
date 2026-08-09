[CmdletBinding()]
param(
    [string]$InstallerPath = ''
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not $InstallerPath) {
    $InstallerPath = Join-Path $repoRoot 'dist\installer\SuperstrikePressure-0.1.0-Setup.exe'
}
$InstallerPath = (Resolve-Path -LiteralPath $InstallerPath).Path
$smokeRoot = Join-Path $repoRoot ('work\installer-smoke\' + [guid]::NewGuid().ToString('N'))
$installRoot = Join-Path $smokeRoot 'Superstrike Pressure'

function Invoke-CheckedProcess {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,
        [int[]]$AllowedExitCodes = @(0)
    )

    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $FilePath
    $startInfo.UseShellExecute = $false
    foreach ($argument in $Arguments) {
        $startInfo.ArgumentList.Add($argument)
    }
    $process = [System.Diagnostics.Process]::Start($startInfo)
    $process.WaitForExit()
    if ($process.ExitCode -notin $AllowedExitCodes) {
        throw "$FilePath exited with code $($process.ExitCode)."
    }
    return $process.ExitCode
}

[void](Invoke-CheckedProcess -FilePath $InstallerPath -Arguments @(
    '/CURRENTUSER',
    '/VERYSILENT',
    '/SUPPRESSMSGBOXES',
    '/NORESTART',
    '/SP-',
    '/TYPE=compact',
    '/COMPONENTS=application',
    '/TASKS=',
    "/DIR=$installRoot"
))

$application = Join-Path $installRoot 'SuperstrikePressure.exe'
$uninstaller = Join-Path $installRoot 'unins000.exe'
if (-not (Test-Path -LiteralPath $application -PathType Leaf)) {
    throw "Installed application is missing: $application"
}
if (-not (Test-Path -LiteralPath $uninstaller -PathType Leaf)) {
    throw "Installed uninstaller is missing: $uninstaller"
}

# A missing recovery state is an expected failure, but proves the installed
# executable can dispatch its embedded watchdog without opening the GUI.
[void](Invoke-CheckedProcess -FilePath $application -Arguments @(
    '--device-restore-watchdog',
    '--parent-pid',
    '2147483647',
    '--state-file',
    (Join-Path $smokeRoot 'missing-state.json')
) -AllowedExitCodes @(1))

[void](Invoke-CheckedProcess -FilePath $uninstaller -Arguments @(
    '/VERYSILENT',
    '/SUPPRESSMSGBOXES',
    '/NORESTART'
))

if (Test-Path -LiteralPath $application) {
    throw "Uninstall left the application behind: $application"
}
Write-Host "Installer lifecycle smoke test passed: $InstallerPath"
