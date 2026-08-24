[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$PackageRoot,
    [Parameter(Mandatory = $true)][string]$RunDirectory,
    [Parameter(Mandatory = $true)][string]$UvPath,
    [Parameter(Mandatory = $true)][string]$PowerShellPath,
    [Parameter(Mandatory = $true)][string]$PythonPath,
    [Parameter(Mandatory = $true)][string]$RunnerCommit,
    [Parameter(Mandatory = $true)][string]$PackageSha256,
    [Parameter(Mandatory = $true)][string]$TargetName,
    [Parameter(Mandatory = $true)][ValidateRange(1, 5)][int]$Repetitions
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$experimentExitCode = 1
$startedAt = [DateTimeOffset]::UtcNow
$evidence = Join-Path $RunDirectory "evidence"
$terminalLog = Join-Path $RunDirectory "terminal.log"
$exitCodePath = Join-Path $RunDirectory "exit-code.json"
$metadataPath = Join-Path $RunDirectory "control-metadata.json"
$resultZip = "$RunDirectory.zip"
$resultShaPath = "$resultZip.sha256"

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
foreach ($requiredFile in @($UvPath, $PowerShellPath, $PythonPath, (Join-Path $PackageRoot "manifest.json"))) {
    if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
        throw "Required Windows Lab file is missing: $requiredFile"
    }
}

$previousPath = $env:PATH
$previousNativeErrorPreference = $PSNativeCommandUseErrorActionPreference
Push-Location $PackageRoot
try {
    $env:PATH = "$(Split-Path $UvPath -Parent);$previousPath"
    $PSNativeCommandUseErrorActionPreference = $false
    $pythonLauncher = "import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); runpy.run_module('experiments.windows_phase0.runner',run_name='__main__')"
    & $PythonPath -c $pythonLauncher $PackageRoot `
        --environment-tier hosted-smoke `
        --pwsh $PowerShellPath `
        --repetitions $Repetitions `
        --runner-commit $RunnerCommit `
        --output-dir $evidence 2>&1 | Tee-Object -FilePath $terminalLog
    $experimentExitCode = $LASTEXITCODE
}
finally {
    $PSNativeCommandUseErrorActionPreference = $previousNativeErrorPreference
    $env:PATH = $previousPath
    Pop-Location
}

[ordered]@{
    experiment_exit_code = $experimentExitCode
} | ConvertTo-Json | Set-Content -LiteralPath $exitCodePath -Encoding utf8

$requiredEvidence = @("environment.json", "observations.jsonl", "summary.json", "summary.md")
$missingEvidence = @()
foreach ($name in $requiredEvidence) {
    if (-not (Test-Path -LiteralPath (Join-Path $evidence $name) -PathType Leaf)) {
        $missingEvidence += $name
    }
}

[ordered]@{
    schema = "tfbash-windows-lab-result/v1"
    run_id = Split-Path $RunDirectory -Leaf
    target_name = $TargetName
    launch_channel = "ssh"
    evidence_tier = "hosted-smoke"
    runner_commit = $RunnerCommit
    package_sha256 = $PackageSha256
    repetitions = $Repetitions
    evidence_complete = ($missingEvidence.Count -eq 0)
    missing_evidence = $missingEvidence
    started_at = $startedAt.ToString("o")
    completed_at = [DateTimeOffset]::UtcNow.ToString("o")
} | ConvertTo-Json | Set-Content -LiteralPath $metadataPath -Encoding utf8

Compress-Archive -Path (Join-Path $RunDirectory "*") -DestinationPath $resultZip
$resultSha256 = (Get-FileHash -LiteralPath $resultZip -Algorithm SHA256).Hash.ToLowerInvariant()
Set-Content -LiteralPath $resultShaPath -Value $resultSha256 -Encoding ascii

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

# The infrastructure succeeded even when the smoke matrix rejected the contract.
exit 0
