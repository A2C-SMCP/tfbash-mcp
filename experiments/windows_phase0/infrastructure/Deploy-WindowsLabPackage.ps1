[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Archive,
    [Parameter(Mandatory = $true)][string]$ExpectedPackageSha256,
    [Parameter(Mandatory = $true)][string]$Destination,
    [Parameter(Mandatory = $true)][string]$ExpectedManifestSha256,
    [Parameter(Mandatory = $true)][string]$PackageSchema,
    [Parameter(Mandatory = $true)][string]$RunnerCommit
)

$ErrorActionPreference = "Stop"
$actualPackageSha256 = (Get-FileHash -LiteralPath $Archive -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actualPackageSha256 -ne $ExpectedPackageSha256) {
    throw "Package checksum mismatch: $actualPackageSha256"
}

if (-not (Test-Path -LiteralPath (Join-Path $Destination "manifest.json"))) {
    if (Test-Path -LiteralPath $Destination) {
        throw "Package destination exists without a manifest: $Destination"
    }
    Expand-Archive -LiteralPath $Archive -DestinationPath $Destination
}

$manifestPath = Join-Path $Destination "manifest.json"
$actualManifestSha256 = (Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actualManifestSha256 -ne $ExpectedManifestSha256) {
    throw "Deployed package manifest checksum mismatch"
}
$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
if ($manifest.schema -ne $PackageSchema -or $manifest.runner_commit -ne $RunnerCommit) {
    throw "Deployed package manifest identity mismatch"
}

$entries = @($manifest.files_sha256.PSObject.Properties)
if ($entries.Count -eq 0) {
    throw "Deployed package manifest has an empty file set"
}
foreach ($entry in $entries) {
    $relative = [string]$entry.Name
    $relativePath = [IO.Path]::GetFullPath((Join-Path $Destination $relative))
    $destinationRoot = [IO.Path]::GetFullPath($Destination).TrimEnd('\') + '\'
    if (-not $relativePath.StartsWith($destinationRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Deployed package manifest contains an unsafe path: $relative"
    }
    if (-not (Test-Path -LiteralPath $relativePath -PathType Leaf)) {
        throw "Deployed package file is missing: $relative"
    }
    $fileHash = (Get-FileHash -LiteralPath $relativePath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($fileHash -ne $entry.Value) {
        throw "Deployed package file checksum mismatch: $relative"
    }
}

$expectedNames = @($entries.Name) + @("manifest.json") | Sort-Object
$actualNames = @(Get-ChildItem -LiteralPath $Destination -File -Recurse | ForEach-Object {
    $_.FullName.Substring($Destination.Length).TrimStart('\').Replace('\', '/')
} | Sort-Object)
if (Compare-Object -ReferenceObject $expectedNames -DifferenceObject $actualNames) {
    throw "Deployed package contains an unexpected or missing file"
}

[ordered]@{
    package_sha256 = $actualPackageSha256
    package_root = $Destination
} | ConvertTo-Json -Compress
