[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$RemoteRoot
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$uvVersion = "0.8.17"
$uvSha256 = "0d051779fbcb173b183efeae1c3e96148764fd82709bbbf0966df3efe48b67c5"
$powerShellVersion = "7.6.3"
$powerShellSha256 = "07ddb0d00b660459560ef82a9841da7705b27cd5dcca5a0d7b025a98eca29eca"
$pythonVersion = "3.12.10"
$pythonSha256 = "4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3"
$pywinptyVersion = "3.0.5"
$pywinptySha256 = "d62946adf14b15b54c0b8d785f93fe18b04da23f4ad59e2e8c4612646e9abd23"

function Remove-StagingDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$Path
    )

    foreach ($attempt in 1..5) {
        if (-not (Test-Path -LiteralPath $Path)) {
            return
        }
        try {
            Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop
            return
        }
        catch {
            if ($attempt -eq 5) {
                throw
            }
            Start-Sleep -Milliseconds 200
        }
    }
}

function Install-VerifiedArchive {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$ExpectedSha256,
        [Parameter(Mandatory = $true)][string]$Destination,
        [Parameter(Mandatory = $true)][string]$ExpectedExecutable
    )

    $downloads = Join-Path $RemoteRoot "downloads"
    New-Item -ItemType Directory -Force -Path $downloads | Out-Null
    $archive = Join-Path $downloads "$Name.zip"
    $temporary = "$Destination.staging-$([Guid]::NewGuid().ToString('N'))"
    try {
        if (-not (Test-Path -LiteralPath $archive -PathType Leaf)) {
            throw "$Name archive was not uploaded by the SSH controller: $archive"
        }
        $archiveHash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($archiveHash -ne $ExpectedSha256) {
            throw "$Name archive checksum mismatch: expected $ExpectedSha256, observed $archiveHash"
        }

        Expand-Archive -LiteralPath $archive -DestinationPath $temporary
        $expectedLeaf = Split-Path $ExpectedExecutable -Leaf
        if (-not (Test-Path -LiteralPath (Join-Path $temporary $expectedLeaf) -PathType Leaf)) {
            throw "$Name archive did not contain the expected executable"
        }
        if (-not (Test-Path -LiteralPath $Destination)) {
            Move-Item -LiteralPath $temporary -Destination $Destination
            return
        }
        if (-not (Test-Path -LiteralPath $Destination -PathType Container)) {
            throw "$Name destination is not a directory: $Destination"
        }

        $referenceFiles = @(Get-ChildItem -LiteralPath $temporary -File -Recurse)
        $installedFiles = @(Get-ChildItem -LiteralPath $Destination -File -Recurse)
        if ($referenceFiles.Count -ne $installedFiles.Count) {
            throw "$Name installed tree file count differs from the verified archive"
        }
        foreach ($reference in $referenceFiles) {
            $relative = $reference.FullName.Substring($temporary.Length).TrimStart('\')
            $installed = Join-Path $Destination $relative
            if (-not (Test-Path -LiteralPath $installed -PathType Leaf)) {
                throw "$Name installed tree is missing file: $relative"
            }
            $referenceHash = (Get-FileHash -LiteralPath $reference.FullName -Algorithm SHA256).Hash
            $installedHash = (Get-FileHash -LiteralPath $installed -Algorithm SHA256).Hash
            if ($referenceHash -ne $installedHash) {
                throw "$Name installed tree checksum mismatch: $relative"
            }
        }
    }
    finally {
        if (Test-Path -LiteralPath $temporary) {
            Remove-StagingDirectory -Path $temporary
        }
    }
}

function Install-VerifiedPythonEnvironment {
    param(
        [Parameter(Mandatory = $true)][string]$Destination
    )

    $downloads = Join-Path $RemoteRoot "downloads"
    $pythonArchive = Join-Path $downloads "python-$pythonVersion-embed-amd64.zip"
    $pywinptyWheel = Join-Path $downloads "pywinpty-$pywinptyVersion-cp312-cp312-win_amd64.whl"
    foreach ($archive in @(
        [ordered]@{ Path = $pythonArchive; Sha256 = $pythonSha256 },
        [ordered]@{ Path = $pywinptyWheel; Sha256 = $pywinptySha256 }
    )) {
        if (-not (Test-Path -LiteralPath $archive.Path -PathType Leaf)) {
            throw "Python environment archive was not uploaded by the SSH controller: $($archive.Path)"
        }
        $actualHash = (Get-FileHash -LiteralPath $archive.Path -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actualHash -ne $archive.Sha256) {
            throw "Python environment archive checksum mismatch: $($archive.Path)"
        }
    }

    $temporary = "$Destination.staging-$([Guid]::NewGuid().ToString('N'))"
    try {
        Expand-Archive -LiteralPath $pythonArchive -DestinationPath $temporary
        $sitePackages = Join-Path $temporary "Lib\site-packages"
        New-Item -ItemType Directory -Force -Path $sitePackages | Out-Null
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        [IO.Compression.ZipFile]::ExtractToDirectory($pywinptyWheel, $sitePackages)

        $pth = Join-Path $temporary "python312._pth"
        $pthLines = @(Get-Content -LiteralPath $pth | Where-Object { $_ -ne "#import site" })
        $pthLines += "Lib\site-packages"
        $pthLines += "import site"
        Set-Content -LiteralPath $pth -Value $pthLines -Encoding ascii

        if (-not (Test-Path -LiteralPath $Destination)) {
            Move-Item -LiteralPath $temporary -Destination $Destination
            return
        }
        if (-not (Test-Path -LiteralPath $Destination -PathType Container)) {
            throw "Python destination is not a directory: $Destination"
        }

        # Python metadata imports can create bytecode after a successful first
        # bootstrap. Remove only generated caches inside this pinned tool root
        # before enforcing an otherwise exact installed-tree comparison.
        Get-ChildItem -LiteralPath $Destination -Directory -Filter "__pycache__" -Recurse |
            Sort-Object { $_.FullName.Length } -Descending |
            Remove-Item -Recurse -Force
        Get-ChildItem -LiteralPath $Destination -File -Filter "*.pyc" -Recurse |
            Remove-Item -Force

        $referenceFiles = @(Get-ChildItem -LiteralPath $temporary -File -Recurse)
        $installedFiles = @(Get-ChildItem -LiteralPath $Destination -File -Recurse)
        if ($referenceFiles.Count -ne $installedFiles.Count) {
            throw "Python installed tree file count differs from the verified archives"
        }
        foreach ($reference in $referenceFiles) {
            $relative = $reference.FullName.Substring($temporary.Length).TrimStart('\')
            $installed = Join-Path $Destination $relative
            if (-not (Test-Path -LiteralPath $installed -PathType Leaf)) {
                throw "Python installed tree is missing file: $relative"
            }
            $referenceHash = (Get-FileHash -LiteralPath $reference.FullName -Algorithm SHA256).Hash
            $installedHash = (Get-FileHash -LiteralPath $installed -Algorithm SHA256).Hash
            if ($referenceHash -ne $installedHash) {
                throw "Python installed tree checksum mismatch: $relative"
            }
        }
    }
    finally {
        if (Test-Path -LiteralPath $temporary) {
            Remove-StagingDirectory -Path $temporary
        }
    }
}

if (-not [Environment]::Is64BitOperatingSystem) {
    throw "Windows Lab requires a 64-bit operating system"
}

$tools = Join-Path $RemoteRoot "tools"
New-Item -ItemType Directory -Force -Path $tools | Out-Null
$uvRoot = Join-Path $tools "uv-$uvVersion"
$uvPath = Join-Path $uvRoot "uv.exe"
$powerShellRoot = Join-Path $tools "powershell-$powerShellVersion"
$powerShellPath = Join-Path $powerShellRoot "pwsh.exe"
$pythonRoot = Join-Path $tools "python-$pythonVersion"
$pythonPath = Join-Path $pythonRoot "python.exe"

Install-VerifiedArchive `
    -Name "uv-$uvVersion" `
    -ExpectedSha256 $uvSha256 `
    -Destination $uvRoot `
    -ExpectedExecutable $uvPath
Install-VerifiedArchive `
    -Name "powershell-$powerShellVersion" `
    -ExpectedSha256 $powerShellSha256 `
    -Destination $powerShellRoot `
    -ExpectedExecutable $powerShellPath
Install-VerifiedPythonEnvironment -Destination $pythonRoot

$uvObserved = (& cmd.exe /d /c ('"' + $uvPath + '" --version') 2>&1 | Out-String).Trim()
$powerShellObserved = (& $powerShellPath -NoLogo -NoProfile -Command '$PSVersionTable.PSVersion.ToString()' 2>&1 | Out-String).Trim()
$pythonObserved = (& cmd.exe /d /c ('"' + $pythonPath + '" --version') 2>&1 | Out-String).Trim()
$previousBytecodePreference = $env:PYTHONDONTWRITEBYTECODE
try {
    $env:PYTHONDONTWRITEBYTECODE = "1"
    $pywinptyObserved = (& cmd.exe /d /c ('"' + $pythonPath + '" -c "import importlib.metadata; print(importlib.metadata.version(''pywinpty''))"') 2>&1 | Out-String).Trim()
}
finally {
    $env:PYTHONDONTWRITEBYTECODE = $previousBytecodePreference
}
if ($uvObserved -notmatch '^uv 0\.8\.17(?:\s|$)') {
    throw "Unexpected uv version: $uvObserved"
}
if ($powerShellObserved -notmatch '^7\.6\.3$') {
    throw "Unexpected PowerShell version: $powerShellObserved"
}
if ($pythonObserved -notmatch '^Python 3\.12\.10$') {
    throw "Unexpected Python version: $pythonObserved"
}
if ($pywinptyObserved -notmatch '^3\.0\.5$') {
    throw "Unexpected pywinpty version: $pywinptyObserved"
}

Write-Output "TFBASH_JSON_BEGIN"
[ordered]@{
    uv_path = $uvPath
    uv_version = $uvObserved
    powershell_path = $powerShellPath
    powershell_version = $powerShellObserved
    python_path = $pythonPath
    python_version = $pythonObserved
    pywinpty_version = $pywinptyObserved
} | ConvertTo-Json -Compress
Write-Output "TFBASH_JSON_END"
