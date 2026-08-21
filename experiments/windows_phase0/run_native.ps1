[CmdletBinding()]
param(
    [string]$PowerShellPath = (Get-Command pwsh -ErrorAction Stop).Source,
    [string]$OutputDirectory = "artifacts/windows-phase0-native"
)

$ErrorActionPreference = "Stop"
$repositoryRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")

Push-Location $repositoryRoot
try {
    uv run --python 3.12 --with pywinpty==3.0.5 -- `
        python -m experiments.windows_phase0.runner `
        --environment-tier native-gate `
        --pwsh $PowerShellPath `
        --repetitions 20 `
        --output-dir $OutputDirectory
    $experimentExitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}

Write-Host "Experiment exit code: $experimentExitCode"
Write-Host "Return the complete output directory even when the exit code is nonzero."
exit $experimentExitCode
