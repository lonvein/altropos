param(
    [Parameter(Mandatory=$true)][string]$Python,
    [Parameter(Mandatory=$true)][string]$EnvFile,
    [Parameter(Mandatory=$true)][string]$StateDirectory,
    [ValidateRange(1024,65535)][int]$ReaderPort = 18000
)
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$Python = (Resolve-Path -LiteralPath $Python).Path
$EnvFile = (Resolve-Path -LiteralPath $EnvFile).Path
$state = New-Item -ItemType Directory -Path $StateDirectory -Force
$stopFile = Join-Path $state.FullName 'stop.request'
if (Test-Path -LiteralPath $stopFile) { throw 'Remove stop.request explicitly before restarting' }
# Exclusive handle prevents two supervisors. It is released on exit/crash.
$lock = [IO.File]::Open((Join-Path $state.FullName 'supervisor.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
$child = $null
try {
    while (-not (Test-Path -LiteralPath $stopFile)) {
        $script = Join-Path $repo 'scripts/deploy/serve.py'
        $launchArguments = '"'+$script+'" --env "'+$EnvFile+'" --port '+$ReaderPort
        $child = Start-Process -FilePath $Python -ArgumentList $launchArguments -WorkingDirectory $repo -WindowStyle Hidden -PassThru
        Set-Content -LiteralPath (Join-Path $state.FullName 'child.pid') -Value $child.Id
        while (-not $child.HasExited -and -not (Test-Path -LiteralPath $stopFile)) {
            Start-Sleep -Seconds 1
            $child.Refresh()
        }
        if (-not $child.HasExited) { Stop-Process -InputObject $child; $child.WaitForExit() }
        $child = $null
        if (-not (Test-Path -LiteralPath $stopFile)) { Start-Sleep -Seconds 10 }
    }
} finally {
    if ($null -ne $child -and -not $child.HasExited) { Stop-Process -InputObject $child }
    $lock.Dispose()
}
