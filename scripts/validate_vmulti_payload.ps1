[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$PayloadRoot,
    [string]$Python = ''
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not $Python) {
    $venvPython = Join-Path $repoRoot '.venv\Scripts\python.exe'
    $Python = if (Test-Path -LiteralPath $venvPython) { $venvPython } else { 'python' }
}
$PayloadRoot = (Resolve-Path -LiteralPath $PayloadRoot).Path

& $Python -m superstrike_pressure.driver_payload $PayloadRoot
if ($LASTEXITCODE -ne 0) {
    throw 'The VMulti payload failed structural or checksum validation.'
}

$manifest = Get-Content -LiteralPath (Join-Path $PayloadRoot 'driver-manifest.json') `
    -Raw | ConvertFrom-Json
$microsoftSignedRoles = @('inf', 'catalog', 'driver')
foreach ($role in $microsoftSignedRoles) {
    $path = Join-Path $PayloadRoot $manifest.$role
    $signature = Get-AuthenticodeSignature -LiteralPath $path
    if ($signature.Status -ne 'Valid') {
        throw "$role signature is not valid: $path ($($signature.Status))"
    }
    if ($signature.SignerCertificate.Subject -notlike `
        '*Microsoft Windows Hardware Compatibility Publisher*') {
        throw "$role was not signed through the Microsoft hardware dashboard: $path"
    }
}

$provisioner = Join-Path $PayloadRoot $manifest.provisioner
$provisionerSignature = Get-AuthenticodeSignature -LiteralPath $provisioner
if ($provisionerSignature.Status -ne 'Valid') {
    throw "The driver provisioner must have a valid Authenticode signature: $provisioner"
}

Write-Host "Validated signed Superstrike VMulti payload: $PayloadRoot"
