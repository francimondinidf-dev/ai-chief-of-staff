# Registers the two Chief of Staff scheduled tasks for the current user.
# Run once from the engine folder:   powershell -ExecutionPolicy Bypass -File .\register_tasks.ps1
#
#   ChiefOfStaff Ingest  07:00, then every 3h   run_cos.bat --refresh  (ingest + extract, sends nothing
#                                                                        unless today's brief was missed)
#   ChiefOfStaff Brief   07:30 daily            run_cos.bat --brief    (full pipeline + email)
#
# Both must run "only when user is logged on" (Interactive): Outlook COM does not work otherwise.
# Battery settings are deliberately permissive. The Windows defaults kill runs on battery
# with exit 0xC000013A and no log line.

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$bat  = Join-Path $root "run_cos.bat"
if (-not (Test-Path $bat)) { throw "run_cos.bat not found next to this script" }

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
$settings  = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -WakeToRun `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 10) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -MultipleInstances IgnoreNew

# Ingest: 07:00, repeating every 3 hours for a day
$ingestTrigger = New-ScheduledTaskTrigger -Daily -At 07:00
$ingestTrigger.Repetition = (New-ScheduledTaskTrigger -Once -At 07:00 `
    -RepetitionInterval (New-TimeSpan -Hours 3) -RepetitionDuration (New-TimeSpan -Hours 24)).Repetition
$ingestTrigger.RandomDelay = "PT5M"
$ingestAction = New-ScheduledTaskAction -Execute $bat -Argument "--refresh" -WorkingDirectory $root
Register-ScheduledTask -TaskName "ChiefOfStaff Ingest" -Action $ingestAction -Trigger $ingestTrigger `
    -Principal $principal -Settings $settings -Force | Out-Null

# Brief: 07:30 daily
$briefTrigger = New-ScheduledTaskTrigger -Daily -At 07:30
$briefAction  = New-ScheduledTaskAction -Execute $bat -Argument "--brief" -WorkingDirectory $root
Register-ScheduledTask -TaskName "ChiefOfStaff Brief" -Action $briefAction -Trigger $briefTrigger `
    -Principal $principal -Settings $settings -Force | Out-Null

Get-ScheduledTask -TaskName "ChiefOfStaff*" | Select-Object TaskName, State
Write-Host "`nRegistered. First brief arrives tomorrow at 07:30 (keep Outlook open)."
