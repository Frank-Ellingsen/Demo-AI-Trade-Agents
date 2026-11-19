param(
    [switch]$Install,
    [switch]$Uninstall,
    [switch]$Status
)

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonExe = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $PythonExe) {
    $PythonExe = "python"
}
$TaskFolder = "\OBX_AI_Trader\"

$TaskSpecs = @(
    @{
        Name = "OBX_PreOpen_0855"
        Description = "OBX 15 før-åpning: utløpssjekk og Telegram HITL-lytter (08:55 CET man-fre)"
        Args = "-c `"from src.hitl.order_queue import expire_stale_orders; print('Expired:', expire_stale_orders())`""
        Days = @("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
        Time = "08:55"
    },
    @{
        Name = "US_PreOpen_1515"
        Description = "US S&P 500 før-åpning: Live Market Bridge BBO & WebSocket sjekk (15:15 CET man-fre)"
        Args = "live_market_bridge.py"
        Days = @("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
        Time = "15:15"
    },
    @{
        Name = "OBX_EOD_Scan_1640"
        Description = "Daglig EOD-skanning Agent 1-4 etter Oslo Børs stengetid (16:40 CET man-fre)"
        Args = "run_daily_scan.py"
        Days = @("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
        Time = "16:40"
    },
    @{
        Name = "Agent5_Sunday_Audit_2000"
        Description = "Ukentlig Agent 5 portefølje- og konseptdrift-revisjon (20:00 CET søndag)"
        Args = "-c `"import subprocess, sys; subprocess.check_call([sys.executable, 'portfolio_monitor.py']); subprocess.check_call([sys.executable, 'setup_drift_views.py'])`""
        Days = @("Sunday")
        Time = "20:00"
    }
)

if ($Install) {
    Write-Host "Installerer Windows Oppgaveplanlegger-oppgaver i $TaskFolder ..." -ForegroundColor Cyan
    foreach ($t in $TaskSpecs) {
        $Action = New-ScheduledTaskAction -Execute $PythonExe -Argument $t.Args -WorkingDirectory $ProjectDir
        $Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $t.Days -At $t.Time
        $Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
        Register-ScheduledTask -TaskPath $TaskFolder -TaskName $t.Name -Action $Action -Trigger $Trigger -Settings $Settings -Description $t.Description -Force | Out-Null
        Write-Host "  [OK] Registrert $($t.Name) ($($t.Time), $($t.Days -join ','))" -ForegroundColor Green
    }
    Write-Host "`nFerdig! Alle 4 automatiske tidsplaner er aktive i Windows Task Scheduler." -ForegroundColor Green
}
elseif ($Uninstall) {
    Write-Host "Fjerner oppgaver i $TaskFolder ..." -ForegroundColor Yellow
    foreach ($t in $TaskSpecs) {
        Unregister-ScheduledTask -TaskPath $TaskFolder -TaskName $t.Name -Confirm:$false -ErrorAction SilentlyContinue
        Write-Host "  [FJERNET] $($t.Name)" -ForegroundColor Yellow
    }
}
else {
    Write-Host "Status for $TaskFolder :" -ForegroundColor Cyan
    Get-ScheduledTask -TaskPath $TaskFolder -ErrorAction SilentlyContinue | Format-Table TaskName, State, @{Label="NextRunTime"; Expression={($_ | Get-ScheduledTaskInfo).NextRunTime}}
    Write-Host "Bruk '.\setup_windows_scheduler.ps1 -Install' for å registrere alle 4 tidsplaner, eller '-Uninstall' for å fjerne dem." -ForegroundColor Gray
}
