[CmdletBinding()]
param(
    [string]$InstallerPath = ''
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not $InstallerPath) {
    $InstallerPath = Get-ChildItem -LiteralPath (Join-Path $repoRoot 'dist\installer') `
        -Filter 'MousePressure-*-Setup.exe' -File |
        Sort-Object LastWriteTimeUtc -Descending |
        Select-Object -First 1 -ExpandProperty FullName
}
if (-not $InstallerPath) {
    throw 'No Mouse Pressure installer was found.'
}
$InstallerPath = (Resolve-Path -LiteralPath $InstallerPath).Path
$smokeRoot = Join-Path $repoRoot ('work\installer-smoke\' + [guid]::NewGuid().ToString('N'))
$installRoot = Join-Path $smokeRoot 'Mouse Pressure'
$installLog = Join-Path $smokeRoot 'install.log'
$uninstallLog = Join-Path $smokeRoot 'uninstall.log'
[void](New-Item -ItemType Directory -Path $smokeRoot -Force)

function Invoke-CheckedProcess {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,
        [int[]]$AllowedExitCodes = @(0),
        [int]$TimeoutSeconds = 180,
        [string]$Phase = 'Process'
    )

    Write-Host "$Phase started: $FilePath"
    $stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $FilePath
    $startInfo.UseShellExecute = $false
    foreach ($argument in $Arguments) {
        $startInfo.ArgumentList.Add($argument)
    }
    $process = [System.Diagnostics.Process]::Start($startInfo)
    if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
        try {
            $process.Kill($true)
        } catch {
            $process.Kill()
        }
        throw "$Phase timed out after $TimeoutSeconds seconds."
    }
    $stopwatch.Stop()
    if ($process.ExitCode -notin $AllowedExitCodes) {
        throw "$Phase exited with code $($process.ExitCode)."
    }
    Write-Host ("$Phase completed in {0:n1}s." -f $stopwatch.Elapsed.TotalSeconds)
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
    "/LOG=$installLog",
    "/DIR=$installRoot"
) -Phase 'Silent install')

$application = Join-Path $installRoot 'MousePressure.exe'
$sandbox = Join-Path $installRoot 'sandbox\MousePressureSandbox.exe'
$uninstaller = Join-Path $installRoot 'unins000.exe'
if (-not (Test-Path -LiteralPath $application -PathType Leaf)) {
    throw "Installed application is missing: $application"
}
if (-not (Test-Path -LiteralPath $sandbox -PathType Leaf)) {
    throw "Installed sandbox is missing: $sandbox"
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
) -AllowedExitCodes @(1) -TimeoutSeconds 30 -Phase 'Packaged watchdog dispatch')

[void](Invoke-CheckedProcess -FilePath $uninstaller -Arguments @(
    '/VERYSILENT',
    '/SUPPRESSMSGBOXES',
    '/NORESTART',
    "/LOG=$uninstallLog"
) -Phase 'Silent uninstall')

if (Test-Path -LiteralPath $application) {
    throw "Uninstall left the application behind: $application"
}
Write-Host "Installer lifecycle smoke test passed: $InstallerPath"
