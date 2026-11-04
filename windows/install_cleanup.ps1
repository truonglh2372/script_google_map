# Tu don o cho crawler gmaps (an_uong_quanh_day#1388, sau disk_low 04/11): Task Scheduler chay disk_cleanup.py moi 15 phut;
# script tu quyet: don dinh ky moi AQD_CLEANUP_EVERY_H gio (6) hoac ngay khi o < AQD_CLEANUP_MIN_FREE_GB (8).
# Chi xoa thu tao lai duoc (xem dau disk_cleanup.py); moi lan don gui 1 dong Slack. Log: $Root\cleanup.log
#   powershell -ExecutionPolicy Bypass -File windows\install_cleanup.ps1            # cai / cai lai
#   powershell -ExecutionPolicy Bypass -File windows\install_cleanup.ps1 -Uninstall # go
#   powershell -ExecutionPolicy Bypass -File windows\install_cleanup.ps1 -DryRun    # chi liet ke, khong cai, khong xoa
param([switch]$Uninstall, [switch]$DryRun, [int]$Minutes = 15)
$ErrorActionPreference = 'Stop'
$Root = if ($env:AQD_ROOT) { $env:AQD_ROOT } else { 'C:\aqd' }
$Task = 'aqd_cleanup_gmaps'
if ($Uninstall) {
  Unregister-ScheduledTask -TaskName $Task -Confirm:$false -ErrorAction SilentlyContinue
  "Da go $Task"; return
}
$Mod = "$Root\disk_cleanup.py"
if (-not (Test-Path $Mod)) { throw "Khong thay $Mod - cai crawler truoc" }
$Py = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $Py) { $Py = 'C:\Users\Admin\AppData\Local\Programs\Python\Python312\pythonw.exe' }
if (-not (Test-Path $Py)) { throw "Khong thay pythonw.exe" }
if ($DryRun) {
  $PyCon = $Py -replace 'pythonw\.exe$', 'python.exe'
  & $PyCon $Mod --crawler gmaps --root $Root --dry-run; return
}
$Arg = "`"$Mod`" --crawler gmaps --root `"$Root`" --log `"$Root\cleanup.log`""
$Action = New-ScheduledTaskAction -Execute $Py -Argument $Arg -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) -RepetitionInterval (New-TimeSpan -Minutes $Minutes)
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
  -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -Priority 7
Register-ScheduledTask -TaskName $Task -Action $Action -Trigger $Trigger -Settings $Settings -Force | Out-Null
"Da cai $Task (kiem tra moi $Minutes phut). Log: $Root\cleanup.log | Go: -Uninstall"
