[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$PackageRoot,
    [Parameter(Mandatory = $true)][string]$RunDirectory,
    [Parameter(Mandatory = $true)][string]$PowerShellPath,
    [Parameter(Mandatory = $true)][string]$PythonPath,
    [Parameter(Mandatory = $true)][string]$SourceCommit,
    [Parameter(Mandatory = $true)][string]$PackageSha256,
    [Parameter(Mandatory = $true)][string]$TargetName,
    [Parameter(Mandatory = $true)][ValidateSet("hosted-smoke", "native-gate")][string]$EvidenceTier,
    [Parameter(Mandatory = $true)][ValidateRange(1, 20)][int]$Repetitions
)

$ErrorActionPreference = "Stop"
if ($EvidenceTier -eq "hosted-smoke" -and $Repetitions -gt 5) {
    throw "SSH smoke permits only 1-5 repetitions"
}
if ($EvidenceTier -eq "native-gate" -and $Repetitions -ne 20) {
    throw "The supervisor native gate requires exactly 20 repetitions"
}
$experimentExitCode = 1
$startedAt = [DateTimeOffset]::UtcNow
$evidencePath = Join-Path $RunDirectory "supervisor-evidence.json"
$terminalLog = Join-Path $RunDirectory "terminal.log"
$metadataPath = Join-Path $RunDirectory "control-metadata.json"
$exitCodePath = Join-Path $RunDirectory "exit-code.json"
$resultZip = "$RunDirectory.zip"

if (Test-Path -LiteralPath $RunDirectory) {
    if ((Get-ChildItem -LiteralPath $RunDirectory -Force | Select-Object -First 1)) {
        throw "Run directory must be absent or empty: $RunDirectory"
    }
}
else {
    New-Item -ItemType Directory -Path $RunDirectory | Out-Null
}
if (Test-Path -LiteralPath $resultZip) {
    throw "Result archive already exists: $resultZip"
}

$previousNativeErrorPreference = $PSNativeCommandUseErrorActionPreference
Push-Location $PackageRoot
try {
    $PSNativeCommandUseErrorActionPreference = $false
    $launcher = "import runpy,sys;sys.path[:0]=[sys.argv.pop(1),sys.argv.pop(1)];runpy.run_module('experiments.windows_supervisor_native.probe',run_name='__main__')"
    & $PythonPath -c $launcher (Join-Path $PackageRoot "src") $PackageRoot `
        --evidence-tier $EvidenceTier `
        --repetitions $Repetitions `
        --pwsh $PowerShellPath `
        --source-commit $SourceCommit `
        --output $evidencePath 2>&1 | Tee-Object -FilePath $terminalLog
    $experimentExitCode = $LASTEXITCODE
}
finally {
    $PSNativeCommandUseErrorActionPreference = $previousNativeErrorPreference
    Pop-Location
}

[ordered]@{ experiment_exit_code = $experimentExitCode } |
    ConvertTo-Json | Set-Content -LiteralPath $exitCodePath -Encoding utf8
[ordered]@{
    schema = "tfbash-windows-supervisor-result/v1"
    run_id = Split-Path $RunDirectory -Leaf
    target_name = $TargetName
    launch_channel = "ssh"
    evidence_tier = $EvidenceTier
    source_commit = $SourceCommit
    package_sha256 = $PackageSha256
    repetitions = $Repetitions
    evidence_complete = (Test-Path -LiteralPath $evidencePath -PathType Leaf)
    started_at = $startedAt.ToString("o")
    completed_at = [DateTimeOffset]::UtcNow.ToString("o")
} | ConvertTo-Json | Set-Content -LiteralPath $metadataPath -Encoding utf8

Compress-Archive -Path (Join-Path $RunDirectory "*") -DestinationPath $resultZip
$resultSha256 = (Get-FileHash -LiteralPath $resultZip -Algorithm SHA256).Hash.ToLowerInvariant()
Set-Content -LiteralPath "$resultZip.sha256" -Value $resultSha256 -Encoding ascii
Write-Output "TFBASH_JSON_BEGIN"
[ordered]@{
    run_id = Split-Path $RunDirectory -Leaf
    launch_channel = "ssh"
    experiment_exit_code = $experimentExitCode
    repetitions = $Repetitions
    result_zip = $resultZip
    result_sha256 = $resultSha256
} | ConvertTo-Json -Compress
Write-Output "TFBASH_JSON_END"

# A rejected candidate is experiment evidence, not an SSH infrastructure failure.
exit 0
