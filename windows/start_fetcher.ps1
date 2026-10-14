# Auto-start Admin fetcher: dedicated Chrome (CDP 9222, C:\aqd\chrome-profile) then alt.py. Single instance; resumes via alt_step.txt + SAFE_DONE markers.
$log='C:\aqd\start_fetcher.log'; function L($m){ Add-Content $log "$(Get-Date -f 'yyyy-MM-dd HH:mm:ss') $m" }
$mtx = New-Object System.Threading.Mutex($false,'Global\aqd_fetcher_launcher')
if(-not $mtx.WaitOne(0)){ L 'another launcher running; exit'; exit }
try {
  if(Test-Path C:\aqd\STOP_ALL){ L 'STOP_ALL present; not starting'; exit }
  if(Get-CimInstance Win32_Process -Filter "name like 'python%'" | ?{ $_.CommandLine -match 'alt\.py|safe_run\.py' }){ L 'alt/safe_run already running; nothing to do'; exit }
  $up = { try { (Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 http://127.0.0.1:9222/json/version).StatusCode -eq 200 } catch { $false } }
  if(-not (& $up)){
    L 'starting Chrome (CDP 9222)'
    Start-Process 'C:\Program Files\Google\Chrome\Application\chrome.exe' -ArgumentList '--no-first-run','--remote-debugging-address=127.0.0.1','--remote-debugging-port=9222','--user-data-dir="C:\aqd\chrome-profile"','--restore-last-session'
    for($i=0;$i -lt 30 -and -not (& $up);$i++){ Start-Sleep 2 }
    if(-not (& $up)){ L 'Chrome CDP not up after 60s; abort'; exit }
    Start-Sleep 10
  } else { L 'Chrome CDP already up' }
  if(Get-CimInstance Win32_Process -Filter "name like 'python%'" | ?{ $_.CommandLine -match 'alt\.py|safe_run\.py' }){ L 'alt appeared meanwhile; exit'; exit }
  Start-Process -WindowStyle Hidden -FilePath 'C:\Users\Admin\AppData\Local\Programs\Python\Python312\python.exe' -ArgumentList 'C:\aqd\alt.py' -WorkingDirectory C:\aqd
  L 'started alt.py'
} finally { $mtx.ReleaseMutex() }
