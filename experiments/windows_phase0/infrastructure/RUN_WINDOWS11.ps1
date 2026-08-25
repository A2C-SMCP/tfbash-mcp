[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$UvPath,
    [Parameter(Mandatory = $true)][string]$PowerShellPath,
    [string]$OutputDirectory = "artifacts/windows-phase0-native"
)

$ErrorActionPreference = "Stop"
$runnerCommit = "61e36d30ac70893b5dd9bdf0745ef3ae1e50f0d7"
$previousPath = $env:PATH
$previousNativeErrorPreference = $PSNativeCommandUseErrorActionPreference
$experimentExitCode = 1

Push-Location $PSScriptRoot
try {
    $env:PATH = "$(Split-Path $UvPath -Parent);$previousPath"
    $PSNativeCommandUseErrorActionPreference = $false
    & $UvPath run --no-project --python 3.12.10 --with pywinpty==3.0.5 -- `
        python -m experiments.windows_phase0.runner `
        --environment-tier native-gate `
        --pwsh $PowerShellPath `
        --repetitions 20 `
        --runner-commit $runnerCommit `
        --output-dir $OutputDirectory
    $experimentExitCode = $LASTEXITCODE
}
finally {
    $PSNativeCommandUseErrorActionPreference = $previousNativeErrorPreference
    $env:PATH = $previousPath
    Pop-Location
}

Write-Host "Reviewed runner commit: $runnerCommit"
Write-Host "Experiment exit code: $experimentExitCode"
exit $experimentExitCode
