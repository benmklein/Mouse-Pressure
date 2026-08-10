[CmdletBinding()]
param(
    [string]$KritaRoot = 'C:\Program Files\Krita (x64)',
    [string]$PluginPath = '',
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'

function Send-EnvironmentChanged {
    if (-not ('SuperstrikeEnvironmentChangeNotifier' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

public static class SuperstrikeEnvironmentChangeNotifier
{
    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern IntPtr SendMessageTimeout(
        IntPtr window,
        uint message,
        UIntPtr word,
        string text,
        uint flags,
        uint timeout,
        out UIntPtr result);
}
'@
    }

    $result = [UIntPtr]::Zero
    [void][SuperstrikeEnvironmentChangeNotifier]::SendMessageTimeout(
        [IntPtr]0xffff,
        0x001A,
        [UIntPtr]::Zero,
        'Environment',
        0x0002,
        5000,
        [ref]$result
    )
}

$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not $PluginPath) {
    $PluginPath = Join-Path $repoRoot 'dist\krita\5.3.3\kritatoolsuperstrikeink.dll'
}

$builtInPlugins = Join-Path $KritaRoot 'lib\kritaplugins'
$userPluginRoot = Join-Path $env:LOCALAPPDATA 'SuperstrikeKritaPlugins'
$builtInJunction = Join-Path $userPluginRoot 'builtin'
$installedPlugin = Join-Path $userPluginRoot 'kritatoolsuperstrikeink.dll'
$userPicsRoot = Join-Path $env:APPDATA 'krita\pics'
$userActionsRoot = Join-Path $env:APPDATA 'krita\actions'
$actionName = 'superstrike_raster_ink.action'
$noticeName = 'THIRD_PARTY_NOTICES.md'
$iconNames = @(
    'superstrike_mouse.png',
    'dark_superstrike_mouse.png',
    'light_superstrike_mouse.png'
)
$currentOverride = [Environment]::GetEnvironmentVariable('KRITA_PLUGIN_PATH', 'User')

if ($Uninstall) {
    if ($currentOverride -eq $userPluginRoot) {
        [Environment]::SetEnvironmentVariable('KRITA_PLUGIN_PATH', $null, 'User')
        Send-EnvironmentChanged
    }
    if (Test-Path -LiteralPath $installedPlugin) {
        Remove-Item -LiteralPath $installedPlugin -Force
    }
    foreach ($iconName in $iconNames) {
        $installedIcon = Join-Path $userPicsRoot $iconName
        if (Test-Path -LiteralPath $installedIcon) {
            Remove-Item -LiteralPath $installedIcon -Force
        }
    }
    $installedAction = Join-Path $userActionsRoot $actionName
    if (Test-Path -LiteralPath $installedAction) {
        Remove-Item -LiteralPath $installedAction -Force
    }
    $installedNotice = Join-Path $userPluginRoot $noticeName
    if (Test-Path -LiteralPath $installedNotice) {
        Remove-Item -LiteralPath $installedNotice -Force
    }
    if (Test-Path -LiteralPath $builtInJunction) {
        $junction = Get-Item -LiteralPath $builtInJunction -Force
        if ($junction.LinkType -ne 'Junction') {
            throw "Refusing to remove non-junction path: $builtInJunction"
        }
        Remove-Item -LiteralPath $builtInJunction -Force
    }
    if ((Test-Path -LiteralPath $userPluginRoot) -and
        -not (Get-ChildItem -LiteralPath $userPluginRoot -Force)) {
        Remove-Item -LiteralPath $userPluginRoot -Force
    }
    Write-Host 'Superstrike Raster Ink was uninstalled. Restart Krita.'
    return
}

if (-not (Test-Path -LiteralPath $PluginPath -PathType Leaf)) {
    throw "Plugin DLL does not exist: $PluginPath"
}
if (-not (Test-Path -LiteralPath $builtInPlugins -PathType Container)) {
    throw "Krita plugin directory does not exist: $builtInPlugins"
}
if ($currentOverride -and $currentOverride -ne $userPluginRoot) {
    throw "KRITA_PLUGIN_PATH already points elsewhere: $currentOverride"
}

New-Item -ItemType Directory -Path $userPluginRoot -Force | Out-Null
New-Item -ItemType Directory -Path $userPicsRoot -Force | Out-Null
New-Item -ItemType Directory -Path $userActionsRoot -Force | Out-Null
if (Test-Path -LiteralPath $builtInJunction) {
    $junction = Get-Item -LiteralPath $builtInJunction -Force
    if ($junction.LinkType -ne 'Junction' -or
        [string]$junction.Target -ne $builtInPlugins) {
        throw "Unexpected path already exists at $builtInJunction"
    }
} else {
    New-Item -ItemType Junction -Path $builtInJunction -Target $builtInPlugins |
        Out-Null
}

try {
    Copy-Item -LiteralPath $PluginPath -Destination $installedPlugin -Force
    $pluginDirectory = Split-Path -Parent $PluginPath
    foreach ($iconName in $iconNames) {
        $sourceIcon = Join-Path $pluginDirectory $iconName
        if (-not (Test-Path -LiteralPath $sourceIcon -PathType Leaf)) {
            throw "Plugin icon does not exist: $sourceIcon"
        }
        Copy-Item -LiteralPath $sourceIcon `
            -Destination (Join-Path $userPicsRoot $iconName) -Force
    }
    $sourceAction = Join-Path $pluginDirectory $actionName
    if (-not (Test-Path -LiteralPath $sourceAction -PathType Leaf)) {
        throw "Plugin action definition does not exist: $sourceAction"
    }
    Copy-Item -LiteralPath $sourceAction `
        -Destination (Join-Path $userActionsRoot $actionName) -Force
    $sourceNotice = Join-Path $pluginDirectory $noticeName
    if (-not (Test-Path -LiteralPath $sourceNotice -PathType Leaf)) {
        throw "Plugin third-party notice does not exist: $sourceNotice"
    }
    Copy-Item -LiteralPath $sourceNotice `
        -Destination (Join-Path $userPluginRoot $noticeName) -Force
} catch [System.IO.IOException] {
    throw "Krita is using the installed Superstrike plugin. Save your work, fully exit Krita, then run this installer again."
}
[Environment]::SetEnvironmentVariable(
    'KRITA_PLUGIN_PATH',
    $userPluginRoot,
    'User'
)
Send-EnvironmentChanged

$hash = (Get-FileHash -LiteralPath $installedPlugin -Algorithm SHA256).Hash
Write-Host "Installed $installedPlugin"
Write-Host "SHA256 $hash"
Write-Host 'Restart Krita to load the tool.'
