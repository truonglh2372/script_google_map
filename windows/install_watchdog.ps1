# Watchdog cho crawler Google Maps (an_uong_quanh_day#1388): Task Scheduler chạy C:\aqd\watchdog.py mỗi 15 phút.
# alt.py chết (và không có C:\aqd\STOP_ALL) -> chạy lại Startup\aqd_fetcher.cmd (start_fetcher.ps1) rồi báo Slack.
#   powershell -ExecutionPolicy Bypass -File windows\install_watchdog.ps1            # cài / cài lại
#   powershell -ExecutionPolicy Bypass -File windows\install_watchdog.ps1 -Uninstall # gỡ
param([switch]$Uninstall, [int]$Minutes = 15)
$ErrorActionPreference = 'Stop'
$Root = if ($env:AQD_ROOT) { $env:AQD_ROOT } else { 'C:\aqd' }
$Task = 'aqd_watchdog_gmaps'
if ($Uninstall) {
  Unregister-ScheduledTask -TaskName $Task -Confirm:$false -ErrorAction SilentlyContinue
  "Da go $Task"; return
}
$Py = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $Py) { $Py = 'C:\Users\Admin\AppData\Local\Programs\Python\Python312\pythonw.exe' }
if (-not (Test-Path $Py)) { throw "Khong thay pythonw.exe" }
if (-not (Test-Path "$Root\watchdog.py")) { throw "Khong thay $Root\watchdog.py - chay windows\install.ps1 truoc" }
$Cmd = Join-Path ([Environment]::GetFolderPath('Startup')) 'aqd_fetcher.cmd'
if (-not (Test-Path $Cmd)) { throw "Khong thay $Cmd - chay windows\install.ps1 truoc" }
$Arg = "`"$Root\watchdog.py`" --crawler gmaps --state-dir `"$Root`" --match `"alt\.py`" " +
       "--restart `"\`"$Cmd\`"`" --stop-file `"$Root\STOP_ALL`" --log `"$Root\watchdog.log`""
$Action = New-ScheduledTaskAction -Execute $Py -Argument $Arg -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $Minutes)
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
  -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName $Task -Action $Action -Trigger $Trigger -Settings $Settings -Force | Out-Null
"Da cai $Task (moi $Minutes phut). Log: $Root\watchdog.log | Go: -Uninstall"
