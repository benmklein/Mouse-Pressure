[CmdletBinding()]
param(
    [string]$BuildRoot = 'E:\kd',
    [string]$KritaVersion = '5.3.3'
)

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$pluginSource = Join-Path $repoRoot 'integrations\krita\superstrike_raster_ink'
$kritaSource = Join-Path $BuildRoot 'krita'
$kritaBuild = Join-Path $BuildRoot 'b_krita'
$envFile = Join-Path $BuildRoot 'env.bat'
$pluginTarget = Join-Path $kritaSource 'plugins\tools\tool_superstrike_ink'
$toolsCMake = Join-Path $kritaSource 'plugins\tools\CMakeLists.txt'
$builtPlugin = Join-Path $kritaBuild 'bin\kritatoolsuperstrikeink.dll'
$distDirectory = Join-Path $repoRoot "dist\krita\$KritaVersion"

foreach ($requiredPath in @($pluginSource, $kritaSource, $envFile, $toolsCMake)) {
    if (-not (Test-Path -LiteralPath $requiredPath)) {
        throw "Required path does not exist: $requiredPath"
    }
}

New-Item -ItemType Directory -Path $pluginTarget -Force | Out-Null
Get-ChildItem -LiteralPath $pluginSource -File |
    Copy-Item -Destination $pluginTarget -Force

$subdirectoryEntry = 'add_subdirectory( tool_superstrike_ink )'
$cmakeText = Get-Content -LiteralPath $toolsCMake -Raw
if (-not $cmakeText.Contains($subdirectoryEntry)) {
    $anchor = 'add_subdirectory( tool_dyna )'
    if (-not $cmakeText.Contains($anchor)) {
        throw "Could not locate the tool_dyna CMake entry in $toolsCMake"
    }
    $cmakeText = $cmakeText.Replace(
        $anchor,
        "$anchor`r`n$subdirectoryEntry"
    )
    [System.IO.File]::WriteAllText(
        $toolsCMake,
        $cmakeText,
        [System.Text.UTF8Encoding]::new($false)
    )
}

if (-not (Test-Path -LiteralPath (Join-Path $kritaBuild 'build.ninja'))) {
    $configureArguments = @(
        "-S `"$kritaSource`"",
        "-B `"$kritaBuild`"",
        '-G Ninja',
        '-DCMAKE_BUILD_TYPE=Release',
        "-DCMAKE_INSTALL_PREFIX=$($BuildRoot.Replace('\', '/'))/_install",
        '-DBUILD_TESTING=OFF',
        '-DINSTALL_BENCHMARKS=OFF',
        '-DKRITA_ENABLE_PCH=OFF',
        '-DHIDE_SAFE_ASSERTS=OFF'
    ) -join ' '
    $configure = "call `"$envFile`" && cmake $configureArguments"
    & cmd.exe /d /c $configure
    if ($LASTEXITCODE -ne 0) {
        throw "Krita configuration failed with exit code $LASTEXITCODE"
    }
}

$build = "call `"$envFile`" && ninja -C `"$kritaBuild`" -j8 kritatoolsuperstrikeink"
& cmd.exe /d /c $build
if ($LASTEXITCODE -ne 0) {
    throw "Raster ink build failed with exit code $LASTEXITCODE"
}
if (-not (Test-Path -LiteralPath $builtPlugin)) {
    throw "Build completed without producing $builtPlugin"
}

New-Item -ItemType Directory -Path $distDirectory -Force | Out-Null
$distPlugin = Join-Path $distDirectory 'kritatoolsuperstrikeink.dll'
Copy-Item -LiteralPath $builtPlugin -Destination $distPlugin -Force
foreach ($iconName in @(
    'superstrike_mouse.png',
    'dark_superstrike_mouse.png',
    'light_superstrike_mouse.png'
)) {
    Copy-Item -LiteralPath (Join-Path $pluginSource $iconName) `
        -Destination (Join-Path $distDirectory $iconName) -Force
}
Copy-Item -LiteralPath (Join-Path $pluginSource 'superstrike_raster_ink.action') `
    -Destination (Join-Path $distDirectory 'superstrike_raster_ink.action') -Force
Copy-Item -LiteralPath (Join-Path $pluginSource 'THIRD_PARTY_NOTICES.md') `
    -Destination (Join-Path $distDirectory 'THIRD_PARTY_NOTICES.md') -Force

$hash = (Get-FileHash -LiteralPath $distPlugin -Algorithm SHA256).Hash
Write-Host "Built $distPlugin"
Write-Host "SHA256 $hash"
