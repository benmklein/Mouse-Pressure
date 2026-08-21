[CmdletBinding()]
param(
    [string]$InstallerPath = '',
    [string]$PreviousInstallerPath = ''
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
$initialInstallerPath = $InstallerPath
if ($PreviousInstallerPath) {
    $initialInstallerPath = (Resolve-Path -LiteralPath $PreviousInstallerPath).Path
}
$smokeRoot = Join-Path $repoRoot ('work\installer-smoke\' + [guid]::NewGuid().ToString('N'))
$installRoot = Join-Path $smokeRoot 'Mouse Pressure'
$installLog = Join-Path $smokeRoot 'install.log'
$upgradeLog = Join-Path $smokeRoot 'upgrade.log'
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

function Wait-FileUnlocked {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [int]$TimeoutSeconds = 10
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        try {
            $stream = [System.IO.File]::Open(
                $Path,
                [System.IO.FileMode]::Open,
                [System.IO.FileAccess]::Read,
                [System.IO.FileShare]::None
            )
            $stream.Dispose()
            return
        } catch [System.IO.IOException] {
            Start-Sleep -Milliseconds 100
        }
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "File remained locked after ${TimeoutSeconds}s: $Path"
}

[void](Invoke-CheckedProcess -FilePath $initialInstallerPath -Arguments @(
    '/CURRENTUSER',
    '/VERYSILENT',
    '/SUPPRESSMSGBOXES',
    '/NORESTART',
    '/SP-',
    '/TASKS=',
    "/LOG=$installLog",
    "/DIR=$installRoot"
) -Phase 'Silent initial install')

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

# Exercise the same-AppId install-over-install path used by version upgrades.
# Generated payload files removed by a newer build must not survive, while
# unrelated user-owned files in the installation directory remain untouched.
$obsoletePayload = Join-Path $installRoot '_internal\obsolete-upgrade-test.txt'
$userOwnedFile = Join-Path $installRoot 'user-owned-upgrade-test.txt'
Set-Content -LiteralPath $obsoletePayload -Value 'obsolete payload'
Set-Content -LiteralPath $userOwnedFile -Value 'preserve me'

[void](Invoke-CheckedProcess -FilePath $InstallerPath -Arguments @(
    '/CURRENTUSER',
    '/VERYSILENT',
    '/SUPPRESSMSGBOXES',
    '/NORESTART',
    '/SP-',
    '/TASKS=',
    "/LOG=$upgradeLog",
    "/DIR=$installRoot"
) -Phase 'Silent install-over-install upgrade')

if (Test-Path -LiteralPath $obsoletePayload) {
    throw "Upgrade left an obsolete packaged file behind: $obsoletePayload"
}
if (-not (Test-Path -LiteralPath $userOwnedFile -PathType Leaf)) {
    throw "Upgrade removed a user-owned file: $userOwnedFile"
}
if (-not (Test-Path -LiteralPath $application -PathType Leaf)) {
    throw "Upgrade removed the installed application: $application"
}
if (-not (Test-Path -LiteralPath $sandbox -PathType Leaf)) {
    throw "Upgrade removed the installed sandbox: $sandbox"
}
if (-not (Test-Path -LiteralPath $uninstaller -PathType Leaf)) {
    throw "Upgrade removed the uninstaller: $uninstaller"
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
Wait-FileUnlocked -Path $application

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
