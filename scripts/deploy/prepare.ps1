param(
    [Parameter(Mandatory=$true)][string]$Python,
    [Parameter(Mandatory=$true)][string]$EnvironmentPath
)
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
if (Test-Path -LiteralPath $EnvironmentPath) { throw 'Choose a NEW environment directory' }
& $Python -m venv $EnvironmentPath
if ($LASTEXITCODE -ne 0) { throw 'venv creation failed' }
$interpreter = Join-Path $EnvironmentPath 'Scripts/python.exe'
& $interpreter -m pip install --no-cache-dir --only-binary=:all: -r (Join-Path $repo 'requirements-app.txt')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed; no services changed' }
& $interpreter -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Dependency consistency check failed' }
Write-Output "Prepared $interpreter. No services started."
