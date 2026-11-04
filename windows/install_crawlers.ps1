# One command per Windows PC for the crawler Slack reports (an_uong_quanh_day#1388). Idempotent: run it again any time.
#   irm https://raw.githubusercontent.com/truonglh2372/script_google_map/main/windows/install_crawlers.ps1 | iex
# It asks for the Slack webhook, the machine name and (optional) the Slack user ID to tag; Enter keeps what
# C:\aqd\crawler.env already has. Then: latest code (C:\script_foody via zip like its install.ps1, C:\aqd payload,
# both backed up first), watchdogs (Task Scheduler, every 15 min, restart + Slack), restart of the crawlers (unless
# their STOP / STOP_ALL file exists), one Slack test message. The webhook is never printed.
# ASCII only on purpose (Windows PowerShell 5.1 reads a .ps1 without a BOM as ANSI).
$ErrorActionPreference = 'Stop'
$Aqd = if ($env:AQD_ROOT) { $env:AQD_ROOT } else { 'C:\aqd' }
$Foody = if ($env:AQD_FOODY_ROOT) { $env:AQD_FOODY_ROOT } else { 'C:\script_foody' }
$Ref = if ($env:AQD_REPO_REF) { $env:AQD_REPO_REF } else { 'main' }
$EnvF = Join-Path $Aqd 'crawler.env'
$TS = Get-Date -Format 'yyyyMMdd_HHmmss'
$Startup = [Environment]::GetFolderPath('Startup')
function Say($m) { Write-Host "[aqd] $m" }
$Webhook = ''; $Quiet = $false
if ($env:AQD_WEBHOOK_FILE) {
  $Quiet = $true
  if (Test-Path $env:AQD_WEBHOOK_FILE) { $Webhook = (Get-Content $env:AQD_WEBHOOK_FILE -Raw).Trim(); Remove-Item $env:AQD_WEBHOOK_FILE -Force }
}

# 1. crawler.env
New-Item -ItemType Directory -Force -Path $Aqd | Out-Null
if (-not (Test-Path $EnvF)) {
  @(
    '# Cau hinh crawler cho MAY NAY (an_uong_quanh_day#1388). File that KHONG commit.',
    '# Windows C:\aqd\crawler.env | Mac ~/aqd/crawler.env (chi chu may doc duoc).',
    '# Sua bang Notepad roi luu; crawler doc lai khi khoi dong, watchdog doc lai moi 15 phut.',
    '# Hoac chay lai lenh cai: no hoi lai tung gia tri, Enter = giu nguyen.',
    '',
    '# Webhook Slack chung cua kenh crawler: https://hooks.slack.com/services/...',
    'SLACK_CRAWLER_WEBHOOK_URL=',
    '',
    '# Ten may hien trong tin Slack, vi du: Admin, Mac mini, TRUONGLE',
    'AQD_HOST_LABEL=',
    '',
    '# Slack user ID (U... hoac W...) duoc tag khi loi nang. De trong = khong tag ai.',
    'SLACK_ALERT_MENTION='
  ) | Set-Content -Path $EnvF -Encoding ASCII
  Say "created $EnvF"
}
function GetKey($k) { $v = ''; foreach ($l in (Get-Content $EnvF -Encoding UTF8)) { if ($l -notmatch '^\s*#' -and $l -match "^\s*$k\s*=(.*)$") { $v = $Matches[1].Trim() } }; $v }
function SetKey($k, $v) {
  $lines = @(Get-Content $EnvF -Encoding UTF8); $done = $false
  for ($i = 0; $i -lt $lines.Count; $i++) { if (-not $done -and $lines[$i] -notmatch '^\s*#' -and $lines[$i] -match "^\s*$k\s*=") { $lines[$i] = "$k=$v"; $done = $true } }
  if (-not $done) { $lines += "$k=$v" }
  [IO.File]::WriteAllLines($EnvF, [string[]]$lines, (New-Object Text.UTF8Encoding($false)))
}
function Mask($v) { if (-not $v) { '(trong)' } elseif ($v.Length -gt 28) { $v.Substring(0, 24) + '...' + $v.Substring($v.Length - 4) } else { '***' } }
$curW = GetKey 'SLACK_CRAWLER_WEBHOOK_URL'; $curN = GetKey 'AQD_HOST_LABEL'; $curM = GetKey 'SLACK_ALERT_MENTION'
if (-not $curN) { $curN = $env:COMPUTERNAME }
$Name = if ($env:AQD_SET_NAME) { $env:AQD_SET_NAME } else { '' }
if (-not $Quiet -and -not [Environment]::UserInteractive) { $Quiet = $true }
if (-not $Quiet -and ([Environment]::GetCommandLineArgs() -match '^-NonI')) { $Quiet = $true; Say 'no console: keeping crawler.env values' }
if (-not $Quiet) {
  while ($true) {
    $r = Read-Host "Webhook Slack (https://hooks.slack.com/services/...), Enter = giu [$(Mask $curW)]"
    if (-not $r -or $r.Trim().StartsWith('https://hooks.slack.com/')) { $Webhook = "$r".Trim(); break }
    Write-Host '  phai bat dau bang https://hooks.slack.com/'
  }
  if (-not $Name) { $r = Read-Host "Ten may hien tren Slack [$curN]"; $Name = if ($r) { $r.Trim() } else { $curN } }
  $r = Read-Host "Slack user ID de tag khi loi nang (U.../W..., '-' = xoa), Enter = giu [$(if ($curM) { $curM } else { 'trong' })]"
  if ($r -eq '-') { SetKey 'SLACK_ALERT_MENTION' '' } elseif ($r) { SetKey 'SLACK_ALERT_MENTION' $r.Trim() }
}
if ($Webhook) { SetKey 'SLACK_CRAWLER_WEBHOOK_URL' $Webhook; Say 'webhook saved (hidden)' }
if (-not $Name) { $Name = $curN }
SetKey 'AQD_HOST_LABEL' $Name
$Sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
icacls $EnvF /inheritance:r /grant:r "*${Sid}:F" | Out-Null   # owner only (by SID: a user named like the PC is ambiguous)
if (-not (GetKey 'SLACK_CRAWLER_WEBHOOK_URL')) { Say "WARNING: SLACK_CRAWLER_WEBHOOK_URL is empty in $EnvF - nothing will be sent" }
Say "host label: $Name"
# Older code reads %APPDATA%\aqd\slack_webhook: hard-link it to crawler.env (one file to edit).
$Old = Join-Path $env:APPDATA 'aqd\slack_webhook'
New-Item -ItemType Directory -Force -Path (Split-Path $Old) | Out-Null
if (Test-Path $Old) { Move-Item $Old "$Old.bak_$TS" -Force; Say "old $Old kept as .bak_$TS" }
try { New-Item -ItemType HardLink -Path $Old -Target $EnvF | Out-Null } catch { Say "note: could not link $Old ($($_.Exception.Message))" }

# 2. code
$Src = Join-Path $env:TEMP "aqd_gm_$TS"
Invoke-WebRequest "https://codeload.github.com/truonglh2372/script_google_map/zip/refs/heads/$Ref" -OutFile "$Src.zip" -UseBasicParsing
Expand-Archive "$Src.zip" $Src -Force; $G = (Get-ChildItem $Src -Directory | Select-Object -First 1).FullName
if (Test-Path "$Foody\common\foody_fetch.py") {
  $B = "C:\foody_backup_${TS}_pre1388"; New-Item -ItemType Directory -Force -Path $B | Out-Null
  Get-ChildItem $Foody -Force | Where-Object { $_.Name -notin @('.venv', 'foody_data') } | Copy-Item -Destination $B -Recurse -Force
  $fz = Join-Path $env:TEMP "aqd_foody_$TS"
  Invoke-WebRequest "https://codeload.github.com/truonglh2372/script_foody/zip/refs/heads/$Ref" -OutFile "$fz.zip" -UseBasicParsing
  Expand-Archive "$fz.zip" $fz -Force; $F = (Get-ChildItem $fz -Directory | Select-Object -First 1).FullName
  Copy-Item "$F\*" $Foody -Recurse -Force   # keeps foody_data\ (progress) and .venv
  if (Test-Path "$Foody\.venv\Scripts\python.exe") { & "$Foody\.venv\Scripts\python.exe" -m pip install -q -r "$Foody\requirements.txt" 2>$null | Out-Null }
  Remove-Item "$fz.zip", $fz -Recurse -Force -ErrorAction SilentlyContinue
  Say "script_foody updated (backup $B)"
}
if (Test-Path "$Aqd\alt.py") {
  $B = "$Aqd\bak\pre1388_$TS"; New-Item -ItemType Directory -Force -Path $B | Out-Null; $n = 0
  foreach ($f in @(Get-ChildItem "$G\common\*.py") + @(Get-Item "$G\windows\alt.py")) {
    $d = Join-Path $Aqd $f.Name
    $same = (Test-Path $d) -and (((Get-Content $f.FullName) -join "`n") -eq ((Get-Content $d) -join "`n"))
    if (-not $same) { if (Test-Path $d) { Copy-Item $d $B }; Copy-Item $f.FullName $d -Force; Say "google maps: updated $($f.Name)"; $n++ }
  }
  if ($n -eq 0) { Remove-Item $B } else { Say "backup: $B" }
}

# 3. watchdogs (repo scripts)
if ((Test-Path "$Foody\windows\install_watchdog.ps1") -and (Test-Path "$Startup\aqd_foody.cmd")) { $env:AQD_FOODY_ROOT = $Foody; & powershell -NoProfile -ExecutionPolicy Bypass -File "$Foody\windows\install_watchdog.ps1" }
if ((Test-Path "$Aqd\watchdog.py") -and (Test-Path "$Startup\aqd_fetcher.cmd")) { $env:AQD_ROOT = $Aqd; & powershell -NoProfile -ExecutionPolicy Bypass -File "$G\windows\install_watchdog.ps1" }
# 3b. auto disk cleanup (Task Scheduler, checks every 15 min; cleans every AQD_CLEANUP_EVERY_H h or when disk is low)
if ((Test-Path "$Foody\windows\install_cleanup.ps1") -and (Test-Path "$Foody\common\disk_cleanup.py") -and (Test-Path "$Startup\aqd_foody.cmd")) { $env:AQD_FOODY_ROOT = $Foody; & powershell -NoProfile -ExecutionPolicy Bypass -File "$Foody\windows\install_cleanup.ps1" }
if ((Test-Path "$Aqd\disk_cleanup.py") -and (Test-Path "$Startup\aqd_fetcher.cmd")) { $env:AQD_ROOT = $Aqd; & powershell -NoProfile -ExecutionPolicy Bypass -File "$G\windows\install_cleanup.ps1" }

# 4. restart
if ($env:AQD_RESTART -ne '0') {
  if (Test-Path "$Startup\aqd_foody.cmd") {
    if (Test-Path "$Foody\foody_data\STOP") { Say 'foody: STOP file present - not restarted' } else {
      Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Where-Object { $_.CommandLine -like '*foody_fetch.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
      Start-Sleep 2; Start-Process cmd.exe -ArgumentList '/c', "`"$Startup\aqd_foody.cmd`"" -WindowStyle Hidden; Say 'foody restarted' }
  }
  if (Test-Path "$Startup\aqd_fetcher.cmd") {
    if (Test-Path "$Aqd\STOP_ALL") { Say 'google maps: STOP_ALL present - not restarted' } else {
      $needle = $Aqd.Replace('\', '/').ToLowerInvariant() + '/'
      Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Where-Object { $_.CommandLine -match 'alt\.py|safe_run\.py' -and $_.CommandLine.Replace('\', '/').ToLowerInvariant() -like "*$needle*" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
      Start-Sleep 2; Start-Process cmd.exe -ArgumentList '/c', "`"$Startup\aqd_fetcher.cmd`"" -WindowStyle Hidden; Say 'google maps restarted' }
  }
}
Remove-Item "$Src.zip", $Src -Recurse -Force -ErrorAction SilentlyContinue

# 5. Slack test
if ($env:AQD_SLACK_TEST -ne '0') {
  $url = GetKey 'SLACK_CRAWLER_WEBHOOK_URL'
  if (-not $url) { Say 'slack test: SKIPPED (no webhook)' } else {
    $body = [Text.Encoding]::UTF8.GetBytes((@{ text = "*crawler @ $Name*: da cai bao cao Slack + watchdog (test, #1388)" } | ConvertTo-Json))
    try { $r = Invoke-WebRequest $url -Method Post -Body $body -ContentType 'application/json; charset=utf-8' -UseBasicParsing; Say "slack test: HTTP $($r.StatusCode)" }
    catch { Say "slack test: FAILED $($_.Exception.Response.StatusCode.value__)" }
  }
}
Say "done. Edit $EnvF any time (Notepad)."
