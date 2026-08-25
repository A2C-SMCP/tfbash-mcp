[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$PowerShellPath,
    [Parameter(Mandatory = $true)][string]$PythonPath,
    [Parameter(Mandatory = $true)][string]$SourceCommit,
    [string]$Output = "artifacts/windows-supervisor-native/evidence.json"
)

$ErrorActionPreference = "Stop"
$packageRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$launcher = "import runpy,sys;sys.path[:0]=[sys.argv.pop(1),sys.argv.pop(1)];runpy.run_module('experiments.windows_supervisor_native.probe',run_name='__main__')"
& $PythonPath -c $launcher (Join-Path $packageRoot "src") $packageRoot `
    --evidence-tier native-gate `
    --repetitions 1 `
    --pwsh $PowerShellPath `
    --source-commit $SourceCommit `
    --output $Output
exit $LASTEXITCODE
