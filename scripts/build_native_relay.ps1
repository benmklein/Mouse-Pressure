[CmdletBinding()]
param(
    [string]$Zig = '',
    [switch]$BootstrapZig
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$outputDir = Join-Path $repoRoot 'build\native'
$output = Join-Path $outputDir 'mouse_pressure_synthetic_relay.dll'
$source = Join-Path $repoRoot 'native\synthetic_relay\mouse_pressure_synthetic_relay.cpp'

function Remove-VerifiedToolDirectory([string]$Path) {
    $absolute = [System.IO.Path]::GetFullPath($Path)
    $allowedRoot = [System.IO.Path]::GetFullPath((Join-Path $repoRoot 'work\tools'))
    $allowedPrefix = $allowedRoot.TrimEnd('\') + '\'
    if (-not $absolute.StartsWith($allowedPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove a directory outside work\tools: $absolute"
    }
    Remove-Item -LiteralPath $absolute -Recurse -Force
}

if (-not $Zig) {
    $zigCommand = Get-Command zig.exe -ErrorAction SilentlyContinue
    if ($zigCommand) {
        $Zig = $zigCommand.Source
    }
}

if (-not $Zig) {
    $toolRoot = Join-Path $repoRoot 'work\tools\zig-0.16.0'
    $candidate = Join-Path $toolRoot 'zig.exe'
    if (Test-Path -LiteralPath $candidate -PathType Leaf) {
        $Zig = $candidate
    } elseif ($BootstrapZig) {
        $archive = Join-Path $repoRoot 'work\tools\zig-0.16.0.zip'
        $download = 'https://ziglang.org/download/0.16.0/zig-x86_64-windows-0.16.0.zip'
        $expectedSha256 = '68659eb5f1e4eb1437a722f1dd889c5a322c9954607f5edcf337bc3684a75a7e'
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $archive) | Out-Null
        if (-not (Test-Path -LiteralPath $archive -PathType Leaf)) {
            Write-Host 'Downloading the pinned portable Zig toolchain...'
            Invoke-WebRequest -Uri $download -OutFile $archive
        }
        $actualSha256 = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actualSha256 -ne $expectedSha256) {
            throw "Zig archive checksum mismatch: $actualSha256"
        }
        $extractRoot = Join-Path $repoRoot 'work\tools\zig-extract'
        if (Test-Path -LiteralPath $extractRoot) {
            Remove-VerifiedToolDirectory -Path $extractRoot
        }
        Expand-Archive -LiteralPath $archive -DestinationPath $extractRoot
        $expanded = Get-ChildItem -LiteralPath $extractRoot -Directory | Select-Object -First 1
        if (-not $expanded) { throw 'The Zig archive did not contain a toolchain directory.' }
        Move-Item -LiteralPath $expanded.FullName -Destination $toolRoot
        Remove-VerifiedToolDirectory -Path $extractRoot
        $Zig = $candidate
    }
}

if (-not $Zig -or -not (Test-Path -LiteralPath $Zig -PathType Leaf)) {
    throw 'Zig was not found. Pass -Zig <path> or use -BootstrapZig.'
}

New-Item -ItemType Directory -Force -Path $outputDir | Out-Null
& $Zig c++ $source `
    -target x86_64-windows-gnu `
    -std=c++17 `
    -O3 `
    -shared `
    -fno-exceptions `
    -fno-rtti `
    -nostdlib++ `
    -luser32 `
    -lkernel32 `
    -o $output
if ($LASTEXITCODE -ne 0) { throw 'Native synthetic relay compilation failed.' }

Write-Host "Built native relay: $output"
