<#
.SYNOPSIS
    Register claudejobs services as Scheduled Tasks that start at logon.

.DESCRIPTION
    Jobs open a terminal window each, which requires an interactive desktop
    session. These tasks therefore run at logon as the current user, NOT as a
    service. For a true unattended service, set TERMINAL_MODE=headless in .env
    and change -Logon to -AtStartup with a stored credential.

.EXAMPLE
    # From an elevated PowerShell prompt, in the repo root:
    .\deploy\windows\Register-ClaudeJobsTasks.ps1

.EXAMPLE
    .\deploy\windows\Register-ClaudeJobsTasks.ps1 -Remove
#>
[CmdletBinding()]
param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path,
    [switch]$Remove
)

$ErrorActionPreference = "Stop"

$python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "No virtualenv at $python. Create it first: python -m venv .venv"
}

# Services to register. Bots are skipped when their token is absent from .env.
$envFile = Join-Path $RepoRoot ".env"
$envText = if (Test-Path $envFile) { Get-Content $envFile -Raw } else { "" }

$services = @("api", "dispatcher")
if ($envText -match "(?m)^\s*TELEGRAM_BOT_TOKEN\s*=\s*\S") { $services += "telegram" }
if ($envText -match "(?m)^\s*SLACK_BOT_TOKEN\s*=\s*\S")    { $services += "slack" }

foreach ($service in $services) {
    $taskName = "claudejobs-$service"

    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
        Write-Host "removing existing task $taskName"
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    }
    if ($Remove) { continue }

    $action = New-ScheduledTaskAction -Execute $python `
        -Argument "-m claudejobs $service" -WorkingDirectory $RepoRoot
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    # Restart if it stops, and never stop it for running too long.
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -RestartInterval (New-TimeSpan -Minutes 1) -RestartCount 999
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME `
        -LogonType Interactive -RunLevel Limited

    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal `
        -Description "claudejobs $service (see $RepoRoot\docs\OPERATIONS.md)" | Out-Null
    Write-Host "registered $taskName"
}

if ($Remove) {
    Write-Host "all claudejobs tasks removed"
} else {
    Write-Host ""
    Write-Host "Tasks start at your next logon. To start them now:"
    foreach ($service in $services) { Write-Host "  Start-ScheduledTask -TaskName claudejobs-$service" }
    Write-Host ""
    Write-Host "This machine must stay logged on, because each job opens a terminal window."
    Write-Host "For a windowless setup, set TERMINAL_MODE=headless in .env."
}
