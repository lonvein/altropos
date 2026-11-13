# Writes a reviewable XML only; NEVER registers or runs a task.
param(
    [Parameter(Mandatory=$true)][string]$Python,
    [Parameter(Mandatory=$true)][string]$EnvFile,
    [Parameter(Mandatory=$true)][string]$StateDirectory,
    [Parameter(Mandatory=$true)][string]$TaskUser,
    [Parameter(Mandatory=$true)][string]$OutputXml
)
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$supervisor = Join-Path $PSScriptRoot 'supervise.ps1'
$arguments = '-NoProfile -WindowStyle Hidden -File "'+$supervisor+'" -Python "'+$Python+'" -EnvFile "'+$EnvFile+'" -StateDirectory "'+$StateDirectory+'"'
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arguments -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $TaskUser
$principal = New-ScheduledTaskPrincipal -UserId $TaskUser -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$task = New-ScheduledTask -Action $action -Trigger $trigger -Principal $principal -Settings $settings
$task | Export-ScheduledTask | Set-Content -LiteralPath $OutputXml -Encoding Unicode
Write-Output 'XML prepared only. Review paths/account before importing in Task Scheduler.'
